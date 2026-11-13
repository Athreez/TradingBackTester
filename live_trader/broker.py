"""Real-money broker: the strategy's orders become INDmoney orders, and fills are read back from INDmoney.

Subclasses SimBroker for the bookkeeping (cash, positions, fills, equity), so the bot's journals, snapshot,
Telegram messages and commands work unchanged. Only order handling differs.

Sending (send_pending, after every bar and every poll):
  - an opening SELL (the fly's shorts) waits until every opening BUY (the hedges) has filled; if an
    opening order fails, the shorts are never sent and `entry_failed` tells the runner to flatten
  - a closing order waits until cancels on the same instrument are confirmed, then is capped to what is
    still open, so a stop that filled meanwhile is never bought back twice (capped to 0 it is dropped)
  - stops go out as GTT TRIGGER smart orders, held on INDmoney's servers so they fire with this PC off;
    a stop INDmoney refuses (e.g. price already past the trigger) is sent as a market order instead
  - INDmoney turns MARKET orders into limit orders inside a price band; one still open after
    `chase_seconds` is cancelled and its remainder sent again (at most MAX_ATTEMPTS times)
  - a request that fails without a clear answer (timeout, 5xx) may still have placed the order: it is
    looked up by its `remarks` before anything is sent again
Fills (poll, every few seconds): GET /order-book rows are matched to our orders by id and by `remarks`
(an order created by a triggered GTT inherits the remark). A fill is booked once the order is complete.
Every `reconcile_seconds` GET /portfolio/positions is compared with our positions: a working order that
explains the difference exactly is booked at the day's average price (a triggered GTT that the order book
doesn't show); any other difference that persists halts new entries and alerts.

Refused outright (REJECTED + alert): orders above `max_lots`, and opening orders while halted, from
catch-up bars after a late start, or beyond `max_entries` a day (the fly is 4; retries don't count).
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from backtester.sim_broker import SimBroker
from core.costs import CostModel
from core.models import Fill, Instrument, Order, OrderStatus, OrderType, Side
from data.indmoney_client import IndMoneyError
from live_trader.mirror import PRODUCT, SEGMENT, _num

log = logging.getLogger("live")

TERMINAL = {"SUCCESS", "FAILED", "ABORTED", "CANCELLED", "EXPIRED",
            "PARTIALLY FILLED - CANCELLED", "PARTIALLY FILLED - EXPIRED"}
STOPS = (OrderType.STOP, OrderType.STOP_LIMIT)
UNKNOWN_GRACE = timedelta(seconds=30)  # how long an unanswered order may take to show up in the order book
MAX_ATTEMPTS = 3
STOP_LIMIT_BUFFER = 0.05  # limit above the trigger for a stop sent without one


def _tick(price: float) -> float:
    return round(round(price / 0.05) * 0.05, 2)


@dataclass
class Sent:
    """One order's life on INDmoney."""
    order_id: int  # our Order.id
    remark: str
    kind: str  # "order" (POST /order) or "gtt" (POST /smart/order)
    sent_at: datetime
    qty: int
    opening: bool
    attempt: int = 1
    broker_ids: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    filled: int = 0
    avg_price: float = 0.0
    cancel_requested: bool = False
    cancel_at: datetime | None = None
    stuck_alerted: bool = False
    resend: bool = False  # after the cancel is confirmed, send the remainder again (market chase)
    unknown: bool = False  # the request failed in a way that may still have placed it
    done: bool = False


class LiveBroker(SimBroker):
    TRANSIENT = ("_quote", "client", "codes", "notifier", "now")  # not pickled with the bot's state

    def __init__(self, client, codes: dict[str, str], costs: CostModel, initial_capital: float, log_dir: Path,
                 now: Callable[[], datetime] = datetime.now, max_lots: int = 1, max_entries: int = 4,
                 chase_seconds: int = 60, reconcile_seconds: int = 30, mismatch_checks: int = 3):
        super().__init__(lambda inst: None, costs, initial_capital)
        self.attach(client, codes, now)
        self.log_dir = log_dir
        self.max_lots, self.max_entries = max_lots, max_entries
        self.chase = timedelta(seconds=chase_seconds)
        self.reconcile_every = timedelta(seconds=reconcile_seconds)
        self.mismatch_checks = mismatch_checks
        self.sent: dict[int, Sent] = {}
        self._by_id: dict[int, Order] = {}
        self._opening: dict[int, bool] = {}
        self._outbox: list[int] = []
        self._undelivered: list[Fill] = []
        self._mismatch: dict[str, int] = {}
        self._last_reconcile: datetime | None = None
        self.alerts: list[str] = []  # drained by the runner into Telegram
        self.halted: str | None = None  # no new entries today (closing orders still go out)
        self.entry_failed: str | None = None  # an opening order failed: the runner flattens and stops for the day
        self.accept_opening = True  # False while replaying catch-up bars
        self.day: date | None = None
        self.entries_today = 0  # opening orders the strategy placed today (retries not counted)

    def attach(self, client, codes: dict[str, str], now: Callable[[], datetime]) -> None:
        """(Re)connect the parts that aren't saved with the bot's state."""
        self.client, self.codes, self.now = client, codes, now

    # ---- day ------------------------------------------------------------

    def start_day(self, day: date) -> None:
        if self.day != day:
            self.day, self.entries_today, self.halted, self.entry_failed = day, 0, None, None
            self._mismatch.clear()
        self.accept_opening = True
        for s in self.sent.values():  # GTTs live for a year on INDmoney: nothing from an earlier day stays
            if not s.done and s.sent_at.date() != day:
                self._cancel_sent(s)

    def cancel_everything(self) -> None:
        """Day end: cancel every working order, including GTT stops."""
        for oid in list(self._outbox):
            self.cancel_order(oid)
        for s in self.sent.values():
            if not s.done and not s.cancel_requested:
                self._cancel_sent(s)

    def working(self) -> list[Sent]:
        return [s for s in self.sent.values() if not s.done]

    # ---- Broker ---------------------------------------------------------

    def place_order(self, order: Order) -> Order:
        order.status = OrderStatus.PENDING
        self._orders.append(order)
        self._by_id[order.id] = order
        opening = self._opening[order.id] = self._is_opening(order)
        why = None
        if order.quantity <= 0:
            why = "zero quantity"
        elif order.quantity > self.max_lots * order.instrument.lot_size:
            why = f"{order.quantity} units is more than LIVE_MAX_LOTS={self.max_lots} lots"
        elif opening and self.halted:
            why = f"trading halted: {self.halted}"
        elif opening and not self.accept_opening:
            why = "placed while catching up after a late start"
        elif opening and self.entries_today >= self.max_entries:
            why = f"already placed {self.max_entries} entry orders today (LIVE_MAX_ENTRY_ORDERS)"
        elif opening and self.entry_failed:
            why = f"entry already failed: {self.entry_failed}"
        if why:
            self._reject(order, why)
            return order
        self.entries_today += opening
        self._pending[order.id] = order
        self._outbox.append(order.id)
        return order

    def cancel_order(self, order_id: int) -> None:
        order = self._pending.get(order_id)
        if order is None:
            return
        if order_id in self._outbox:
            self._outbox.remove(order_id)
            del self._pending[order_id]
            order.status = OrderStatus.CANCELLED
            return
        s = self.sent.get(order_id)
        if s and not s.done and not s.cancel_requested:
            self._cancel_sent(s)

    def open_orders(self) -> list[Order]:
        """Orders still working; ones being cancelled are already gone from the strategy's point of view."""
        return [o for o in self._pending.values() if not (o.id in self.sent and self.sent[o.id].cancel_requested)]

    def process(self, ts: datetime) -> list[Fill]:
        """Engine step (run_bar): no matching against candles here, just stop triggers and fills seen so far."""
        self.mark_triggers(ts)
        return self.take_fills()

    def take_fills(self) -> list[Fill]:
        out, self._undelivered = self._undelivered, []
        return out

    def settle(self, instrument: Instrument, price: float, ts: datetime) -> Fill | None:
        log.warning("%s still open at day end: INDmoney settles it, not the bot", instrument.symbol)
        return None

    def mark_triggers(self, ts: datetime) -> None:
        """A stop whose trigger traded in this bar (strategy uses triggered_at to chase an unfilled stop)."""
        for o in self._pending.values():
            if o.order_type in STOPS and o.triggered_at is None:
                bar = self._quote(o.instrument)
                if bar and (bar.high >= o.trigger_price if o.side is Side.BUY else bar.low <= o.trigger_price):
                    o.triggered_at = ts

    # ---- sending --------------------------------------------------------

    def send_pending(self) -> None:
        for oid in list(self._outbox):
            order = self._pending.get(oid)
            if order is None:
                self._outbox.remove(oid)
                continue
            if self._must_wait(order):
                continue
            self._outbox.remove(oid)
            if self._opening[oid] and order.side is Side.SELL and self.entry_failed:
                self._reject(order, f"not sent: {self.entry_failed}")
                continue
            if not self._opening[oid] and not self._cap(order):
                continue
            self._transmit(order)

    def _is_opening(self, order: Order) -> bool:
        if order.order_type in STOPS:
            return False
        pos = self._positions.get(order.instrument)
        qty = pos.quantity if pos else 0
        return qty == 0 or (qty > 0) == (order.side is Side.BUY)

    def _must_wait(self, order: Order) -> bool:
        if self._opening[order.id]:
            if order.side is Side.SELL:  # shorts only after the hedges are in
                return any(self._opening[o.id] and o.side is Side.BUY and o.order_type not in STOPS
                           for o in self._pending.values() if o.id != order.id)
            return False
        return any(s.cancel_requested and self._by_id[s.order_id].instrument == order.instrument
                   for s in self.working())

    def _cap(self, order: Order) -> bool:
        """Closing orders: never more than is open (minus other working closing orders). False = dropped."""
        pos = self._positions.get(order.instrument)
        open_qty = pos.quantity if pos else 0
        closes = (open_qty > 0 and order.side is Side.SELL) or (open_qty < 0 and order.side is Side.BUY)
        others = sum(s.qty - s.filled for s in self.working() if not s.cancel_requested
                     and not s.opening and self._by_id[s.order_id].instrument == order.instrument
                     and self._by_id[s.order_id].side is order.side)
        room = abs(open_qty) - others if closes else 0
        if room <= 0:
            del self._pending[order.id]
            order.status = OrderStatus.CANCELLED
            log.info("dropped %s %s: nothing left to close", order.tag, order.instrument.symbol)
            return False
        if room < order.quantity:
            log.info("%s %s: quantity capped %d -> %d (what is still open)", order.tag, order.instrument.symbol,
                     order.quantity, room)
            order.quantity = room
        return True

    def _transmit(self, order: Order, attempt: int = 1) -> None:
        inst = order.instrument
        code = self.codes.get(inst.symbol)
        if code is None:
            self._reject(order, "no INDmoney scrip code")
            return
        if order.order_type in STOPS and self._past_trigger(order):
            log.warning("%s %s: price already past the stop trigger %.2f, sending at market", order.tag,
                        inst.symbol, order.trigger_price)
            order.order_type = OrderType.MARKET
        exchange_seg, security_id = code.split("_", 1)
        remark = f"vif-{self.day or self.now().date():%m%d}-{order.id}-{order.tag}"[:100]
        body = {"txn_type": order.side.value, "exchange": "BSE" if exchange_seg == "BFO" else "NSE",
                "segment": SEGMENT, "product": PRODUCT, "validity": "DAY", "security_id": security_id,
                "qty": order.quantity, "remarks": remark}
        if order.order_type in STOPS:
            kind, trigger = "gtt", _tick(order.trigger_price)
            limit = order.limit_price or trigger * (1 + STOP_LIMIT_BUFFER if order.side is Side.BUY else 1 - STOP_LIMIT_BUFFER)
            body.update(order_type="TRIGGER", trigger_price=trigger, trigger_limit_price=_tick(limit))
        elif order.order_type is OrderType.LIMIT:
            kind = "order"
            body.update(order_type="LIMIT", limit_price=_tick(order.limit_price))
        else:
            kind = "order"
            body.update(order_type="MARKET")
        s = Sent(order.id, remark, kind, self.now(), order.quantity, self._opening[order.id], attempt)
        self.sent[order.id] = s
        status, error = "", ""
        try:
            data = (self.client.place_order if kind == "order" else self.client.place_smart_order)(body)
        except IndMoneyError as e:
            error = str(e)
            if "HTTP 4" not in error:  # 5xx: the order may or may not exist
                s.unknown = True
            else:  # refused before it reached the exchange
                s.done = True
                if kind == "gtt":  # a short without a stop is not allowed: close it now instead
                    self._alert(f"Stop-loss for {inst.symbol} refused by INDmoney ({error[:150]}); closing at market")
                    del self.sent[order.id]
                    order.order_type = OrderType.MARKET
                    self._transmit(order, attempt)
                    return
                self._reject(order, f"INDmoney refused: {error[:200]}")
        except Exception as e:  # timeout / connection reset: the order may have gone through
            error, s.unknown = str(e), True
        else:
            ids = [data["order_id"]] if data.get("order_id") else []
            status = data.get("order_status", "")
            for leg in data.get("order_data") or []:
                ids.append(leg.get("order_id"))
                status = leg.get("order_status", status)
                ids.append((leg.get("child_order_details") or {}).get("order_id"))
            s.broker_ids = [i for i in ids if i]
            s.unknown = not s.broker_ids
        if s.unknown:
            log.error("LIVE %s %s: no clear answer from INDmoney (%s); looking it up by remark", order.tag,
                      inst.symbol, error or "no order id")
        log.info("LIVE sent %s %s %s %d %s -> %s %s%s", kind, order.side.value, inst.symbol, order.quantity,
                 body["order_type"], ",".join(s.broker_ids) or "-", status, f" ERROR {error[:200]}" if error else "")
        self._csv("orders.csv", {"sent_at": s.sent_at, "order_id": order.id, "tag": order.tag, "symbol": inst.symbol,
                                 "side": order.side.value, "qty": order.quantity, "type": body["order_type"],
                                 "trigger": body.get("trigger_price"), "limit": body.get("trigger_limit_price") or
                                 body.get("limit_price"), "attempt": attempt, "broker_ids": " ".join(s.broker_ids),
                                 "status": status, "error": error[:300]})

    def _past_trigger(self, order: Order) -> bool:
        bar = self._quote(order.instrument)
        if bar is None:
            return False
        return bar.close >= order.trigger_price if order.side is Side.BUY else bar.close <= order.trigger_price

    def _cancel_sent(self, s: Sent) -> None:
        s.cancel_requested = True
        s.cancel_at = s.cancel_at or self.now()
        for bid in s.broker_ids:
            if s.statuses.get(bid) in TERMINAL:
                continue
            try:
                (self.client.cancel_smart_order if bid.startswith("GTT-") else self.client.cancel_order)(bid)
                log.info("LIVE cancel requested %s (%s)", bid, self._by_id[s.order_id].tag)
            except Exception as e:  # usually already filled / closed: the next poll tells
                log.info("LIVE cancel %s not done: %s", bid, e)

    # ---- reading back ---------------------------------------------------

    def poll(self) -> None:
        """Order statuses and fills from INDmoney; chase stuck market orders; check positions now and then."""
        active = self.working()
        if active:
            try:
                book = self.client.order_book()
            except Exception as e:
                log.warning("order book poll failed: %s", e)
                book = None
            if book is not None:
                for s in active:
                    self._update(s, book)
        now = self.now()
        for s in self.working():
            order = self._by_id[s.order_id]
            if (s.kind == "order" and order.order_type is OrderType.MARKET and not s.cancel_requested
                    and not s.unknown and now - s.sent_at >= self.chase):
                log.warning("LIVE %s %s unfilled after %ss; cancelling to resend", order.tag, order.instrument.symbol,
                            int(self.chase.total_seconds()))
                s.resend = True
                self._cancel_sent(s)
            elif s.cancel_requested and not s.stuck_alerted and now - s.cancel_at >= 2 * UNKNOWN_GRACE:
                s.stuck_alerted = True  # orders on this instrument wait for this cancel
                self._alert(f"Cancel of {order.tag} {order.instrument.symbol} not confirmed by INDmoney after "
                            f"{int((now - s.cancel_at).total_seconds())}s; exits on it are waiting. Check the app.")
        if self._last_reconcile is None or now - self._last_reconcile >= self.reconcile_every:
            self.reconcile()

    def _update(self, s: Sent, book: list[dict]) -> None:
        order = self._by_id[s.order_id]
        rows = [r for r in book if r.get("id") in s.broker_ids or (r.get("remarks") or "") == s.remark]
        new_ids = [r.get("id") for r in rows if r.get("id") and r.get("id") not in s.broker_ids]
        s.broker_ids += new_ids
        if s.unknown:
            if rows:
                s.unknown = False
                log.info("LIVE found %s %s on INDmoney after all: %s", order.tag, order.instrument.symbol, new_ids)
            elif self.now() - s.sent_at > UNKNOWN_GRACE:
                s.done = True
                log.warning("LIVE %s %s never reached INDmoney", order.tag, order.instrument.symbol)
                self._finish(order, s, OrderStatus.REJECTED, "never reached INDmoney")
                return
            else:
                return
        if s.cancel_requested and new_ids:  # e.g. the order a triggered GTT created
            self._cancel_sent(s)
        for r in rows:
            bid, status = r.get("id"), r.get("status") or ""
            if s.statuses.get(bid) != status:
                s.statuses[bid] = status
                log.info("LIVE %s %s (%s): %s %s", bid, order.instrument.symbol, order.tag, status, r.get("extra_info") or "")
                self._csv("events.csv", {"seen_at": self.now(), "order_id": order.id, "tag": order.tag,
                                         "symbol": order.instrument.symbol, "broker_id": bid, "status": status,
                                         "extra_info": r.get("extra_info"), "traded_qty": r.get("traded_qty"),
                                         "traded_price": r.get("traded_price"), "remarks": r.get("remarks")})
        traded = [(int(_num(r.get("traded_qty")) or 0), _num(r.get("traded_price")) or 0.0) for r in rows]
        qty, price = max(traded, default=(0, 0.0))  # max, not sum: a GTT and its child may both report the fill
        if qty > s.filled and price > 0:
            s.filled, s.avg_price = qty, price
        if s.filled >= s.qty:
            self._finish(order, s, OrderStatus.FILLED)
        elif rows and all(self._row_done(r) for r in rows):
            reason = "; ".join(str(r.get("extra_info")) for r in rows if r.get("extra_info"))
            self._finish(order, s, OrderStatus.CANCELLED if s.cancel_requested else OrderStatus.REJECTED,
                         reason or ", ".join(f"{r.get('id')} {r.get('status')}" for r in rows))

    @staticmethod
    def _row_done(r: dict) -> bool:
        status = r.get("status")
        # A triggered GTT can read SUCCESS before its order has traded: not done until something trades.
        return status in TERMINAL and not (status == "SUCCESS" and (_num(r.get("traded_qty")) or 0) == 0)

    def _finish(self, order: Order, s: Sent, status: OrderStatus, reason: str = "") -> None:
        s.done = True
        self._pending.pop(order.id, None)
        if s.filled > 0:
            wanted, order.quantity = order.quantity, s.filled  # book what actually traded
            self._undelivered.append(self._record_fill(order, s.avg_price, self.now()))
            order.quantity = wanted
        if s.filled >= order.quantity:
            order.status = OrderStatus.FILLED
            return
        order.status = status
        left = order.quantity - s.filled
        inst = order.instrument
        if s.resend or (status is OrderStatus.REJECTED and not s.opening):
            if s.attempt >= MAX_ATTEMPTS:
                if s.opening:
                    self.entry_failed = self.entry_failed or f"{order.tag} {inst.symbol} unfilled after {s.attempt} tries"
                self._alert(f"Could not {order.side.value} {left} {inst.symbol} ({order.tag}) after {s.attempt} tries"
                            f"{f': {reason}' if reason else ''}. Do it by hand in the INDmoney app.")
                return
            again = Order(inst, order.side, left, OrderType.MARKET, tag=order.tag, created_at=self.now())
            self._by_id[again.id], self._opening[again.id] = again, s.opening
            self._orders.append(again)
            self._pending[again.id] = again
            log.warning("LIVE sending %s %s again (%d left, attempt %d)%s", order.tag, inst.symbol, left,
                        s.attempt + 1, f": {reason}" if reason else "")
            if s.opening or self._cap(again):
                self._transmit(again, s.attempt + 1)
            return
        if status is OrderStatus.REJECTED:
            msg = f"{order.tag} {inst.symbol} rejected by INDmoney{f': {reason}' if reason else ''}"
            if s.opening:
                self.entry_failed = self.entry_failed or msg
            self._alert(msg)

    def _reject(self, order: Order, why: str) -> None:
        self._pending.pop(order.id, None)
        order.status = OrderStatus.REJECTED
        msg = f"{order.tag} {order.instrument.symbol} not placed: {why}"
        if self._opening.get(order.id):
            self.entry_failed = self.entry_failed or msg
        self._alert(msg)

    # ---- positions check ------------------------------------------------

    def reconcile(self) -> None:
        self._last_reconcile = self.now()
        try:
            rows = self.client.positions()
        except Exception as e:
            log.warning("positions check failed: %s", e)
            return
        theirs, avg = {}, {}
        for r in rows:
            if (r.get("product") or "INTRADAY").upper() != PRODUCT:
                continue  # positions you hold yourself (MARGIN / CNC) are not the bot's
            sid = _sid(r.get("security_id"))
            theirs[sid] = int(_num(r.get("net_qty")) or 0)
            avg[sid] = {Side.BUY: _avg(r, "day_buy"), Side.SELL: _avg(r, "day_sell")}
        ours = {self._sid(i): p.quantity for i, p in self.positions().items()}
        diffs = {sid for sid in set(ours) | set(theirs) if ours.get(sid, 0) != theirs.get(sid, 0)}
        for sid in list(self._mismatch):
            if sid not in diffs:
                del self._mismatch[sid]
        for sid in diffs:
            gap = theirs.get(sid, 0) - ours.get(sid, 0)
            seen = self._mismatch.get(sid, 0)
            for s in self.working():
                order = self._by_id[s.order_id]
                left = s.qty - s.filled
                if self._sid(order.instrument) == sid and (left if order.side is Side.BUY else -left) == gap and seen >= 1:
                    price = avg.get(sid, {}).get(order.side) or s.avg_price or order.trigger_price or 0.0
                    log.warning("LIVE %s %s: INDmoney positions show it executed (order book doesn't); booked at %.2f",
                                order.tag, order.instrument.symbol, price)
                    s.filled, s.avg_price = s.qty, price
                    self._finish(order, s, OrderStatus.FILLED)
                    break
            else:
                self._mismatch[sid] = seen + 1
                if seen + 1 >= self.mismatch_checks and not self.halted:
                    self.halted = (f"positions differ from INDmoney (security {sid}: bot {ours.get(sid, 0)}, "
                                   f"INDmoney {theirs.get(sid, 0)})")
                    self._alert(f"{self.halted}. No new entries. Check the INDmoney app; close by hand if needed.")
                continue
            self._mismatch.pop(sid, None)

    def _sid(self, inst: Instrument) -> str:
        return _sid((self.codes.get(inst.symbol) or "_").split("_", 1)[1])

    # ---- records --------------------------------------------------------

    def _alert(self, text: str) -> None:
        log.error("ALERT %s", text)
        self.alerts.append(text)

    def _csv(self, name: str, row: dict) -> None:
        path = self.log_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)


def _sid(value) -> str:
    n = _num(value)
    return str(int(n)) if n is not None else str(value or "")


def _avg(row: dict, prefix: str) -> float | None:
    qty, val = _num(row.get(f"{prefix}_qty")), _num(row.get(f"{prefix}_val"))
    return val / qty if qty and val else None
