"""Shadow live trading: send the paper bot's orders to INDmoney as real orders on an unfunded account.

The strategy and the paper account keep running on SimBroker exactly as before. After each live bar,
every order the strategy just placed is also sent to INDmoney (market orders via POST /order, stop-losses
as GTT smart orders), so with no money in the account each one is rejected for insufficient margin.
Rejections carry no fill price, so for every order we record the 5-level order book at the moment it is
sent and compare what that book would have filled our quantity at with the paper fill. Also recorded:
acceptance or the exact rejection reason, round-trip latency, the limit price INDmoney converts a
MARKET order to, and when each GTT stop triggers on INDmoney's side.

Safety: nothing is sent unless INDmoney reports at most `max_balance` rupees available (checked before
every order); orders from catch-up bars are never sent; a daily order cap; leftover open orders are
cancelled at day end and on restart; and if any order ever fills, the opposite order is sent at once
and mirroring stops for the day.

Output (live_trader/logs/, git-ignored):
  orders.csv      one row per order sent: book at send, our price, response, latency
  events.csv      every status change INDmoney reports for those orders (rejection reasons, GTT triggers)
  compare.csv     paper fill vs the price the live book offered when the order was sent
"""
from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from core.models import Fill, Order, OrderStatus, OrderType, Side
from data.indmoney_client import IndMoneyClient

log = logging.getLogger("live")

SEGMENT = "DERIVATIVE"
PRODUCT = "INTRADAY"  # broker squares off by itself if anything were ever left open
FILLED_STATES = {"SUCCESS", "PARTIALLY FILLED", "PARTIALLY FILLED - CANCELLED", "PARTIALLY FILLED - EXPIRED"}
CLOSED_STATES = FILLED_STATES | {"FAILED", "ABORTED", "CANCELLED", "EXPIRED"}


def _num(value) -> float | None:
    """INDmoney sends numbers as strings, sometimes with Indian digit grouping ("5,82,909")."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def parse_depth(entry: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """(bids, asks) as [(price, qty)], best first, from one /market/quotes/mkt entry."""
    bids, asks = [], []
    md = (entry or {}).get("market_depth", {})
    if "depth" not in md and len(md) == 1:  # live API nests once more: market_depth -> {code: {depth}}
        md = next(iter(md.values()))
    for level in md.get("depth", []):
        for side, out in (("buy", bids), ("sell", asks)):
            p, q = _num(level.get(side, {}).get("price")), _num(level.get(side, {}).get("quantity"))
            if p and q:
                out.append((p, q))
    return bids, asks


def book_price(levels: list[tuple[float, float]], qty: int) -> float | None:
    """Average price our whole quantity would fill at against these levels; None if they are too thin."""
    left, cost = qty, 0.0
    for price, avail in levels:
        take = min(left, avail)
        cost += take * price
        left -= take
        if left <= 0:
            return cost / qty
    return None


def available_balance(funds: dict) -> float:
    """The largest balance INDmoney reports in any form (cash, withdrawable, per-segment, pledged)."""
    values = [_num(funds.get(k)) for k in ("sod_balance", "withdrawal_balance", "pledge_received", "pledge_remained")]
    values += [_num(v) for v in (funds.get("detailed_avl_balance") or {}).values()]
    return max((v for v in values if v is not None), default=0.0)


@dataclass
class Mirrored:
    sim_id: int
    tag: str
    symbol: str
    side: str
    qty: int
    kind: str  # "order" (POST /order) or "gtt" (POST /smart/order)
    sent_at: str
    broker_ids: list[str] = field(default_factory=list)  # parent (and GTT child) ids
    statuses: dict[str, str] = field(default_factory=dict)  # broker id -> last status seen
    book_price: float | None = None  # what the book at send would have filled our qty at
    closed: bool = False


class OrderMirror:
    def __init__(self, client: IndMoneyClient, symbol_to_code: dict[str, str], day: date, out_dir: Path,
                 max_balance: float = 100.0, max_orders: int = 40):
        self.client = client
        self.codes = symbol_to_code  # our symbol -> INDmoney scrip code, e.g. NFO_51011
        self.day = day
        self.log_dir = out_dir / "logs"
        self.state_path = out_dir / "state" / f"mirror_{day:%Y%m%d}.json"
        self.max_balance = max_balance
        self.max_orders = max_orders
        self.orders: dict[int, Mirrored] = {}  # sim order id -> mirrored order
        self.seen: set[int] = set()  # sim order ids already handled (sent or skipped)
        self.fills_seen = 0
        self.halted: str | None = None  # reason mirroring stopped for the day
        self._load()

    # ---- lifecycle -------------------------------------------------------

    def start(self, broker) -> None:
        """Called once the day's contracts are loaded. Orders and fills already in the paper account are
        history: never sent. Anything this mirror left open earlier today is cancelled."""
        self.seen.update(o.id for o in broker.orders())
        self.fills_seen = max(self.fills_seen, len(broker.fills()))
        for m in self.orders.values():
            if not m.closed:
                self._cancel(m, reason="restart")
        self._funds_ok()
        self._save()

    def sync(self, broker, bar_ts: datetime, live: bool) -> None:
        """After each bar: send the strategy's new orders (live bars only), cancel what it cancelled,
        and compare new paper fills with the book captured when their order was sent."""
        try:
            for order in broker.orders():
                if order.id in self.seen:
                    if order.status is OrderStatus.CANCELLED and order.id in self.orders:
                        m = self.orders[order.id]
                        if not m.closed:
                            self._cancel(m, reason="strategy cancelled")
                    continue
                self.seen.add(order.id)
                if not live:
                    log.info("not mirroring %s %s (catch-up bar %s)", order.tag, order.instrument.symbol, bar_ts)
                elif self.halted:
                    log.info("not mirroring %s %s: %s", order.tag, order.instrument.symbol, self.halted)
                else:
                    self._send(order)
            for fill in broker.fills()[self.fills_seen:]:
                self._compare(fill)
            self.fills_seen = len(broker.fills())
        except Exception as e:  # the mirror must never break the paper bot
            log.exception("mirror sync failed: %s", e)
        self._save()

    def poll(self) -> None:
        """Pick up status changes (rejections, GTT triggers) from INDmoney's order book."""
        if not any(not m.closed for m in self.orders.values()):
            return
        try:
            book = {row.get("id"): row for row in self.client.order_book()}
        except Exception as e:
            log.warning("order book poll failed: %s", e)
            return
        for m in self.orders.values():
            if m.closed:
                continue
            for bid in m.broker_ids:
                row = book.get(bid)
                if row is None or m.statuses.get(bid) == row.get("status"):
                    continue
                m.statuses[bid] = row.get("status")
                self._event(m, bid, row)
                if row.get("status") in FILLED_STATES or (_num(row.get("traded_qty")) or 0) > 0:
                    self._unexpected_fill(m, row)
            m.closed = all(s in CLOSED_STATES for s in m.statuses.values()) and len(m.statuses) == len(m.broker_ids)
        self._save()

    def close_day(self) -> None:
        self.poll()
        for m in self.orders.values():
            if not m.closed:
                self._cancel(m, reason="end of day")
        self._save()

    # ---- sending ---------------------------------------------------------

    def _funds_ok(self) -> bool:
        try:
            balance = available_balance(self.client.funds() or {})
        except Exception as e:
            log.error("could not read INDmoney funds (%s): not sending orders", e)
            return False
        if balance > self.max_balance:
            self.halted = (f"INDmoney reports Rs {balance:,.2f} available (limit {self.max_balance:,.0f}); "
                           "orders could really fill, so nothing is sent today")
            log.error(self.halted)
            return False
        return True

    def _send(self, order: Order) -> None:
        inst = order.instrument
        code = self.codes.get(inst.symbol)
        if code is None:
            log.warning("no INDmoney code for %s, not mirroring", inst.symbol)
            return
        if len(self.orders) >= self.max_orders:
            self.halted = f"daily cap of {self.max_orders} mirrored orders reached"
            log.error(self.halted)
            return
        if not self._funds_ok():
            return
        exchange_seg, security_id = code.split("_", 1)
        body = {"txn_type": order.side.value, "exchange": "BSE" if exchange_seg == "BFO" else "NSE",
                "segment": SEGMENT, "product": PRODUCT, "validity": "DAY", "security_id": security_id,
                "qty": order.quantity, "remarks": f"vif-{self.day:%m%d}-{order.id}-{order.tag}"[:100]}
        if order.order_type is OrderType.MARKET:
            kind, body["order_type"] = "order", "MARKET"
        elif order.order_type is OrderType.LIMIT:
            kind, body["order_type"], body["limit_price"] = "order", "LIMIT", order.limit_price
        else:  # STOP / STOP_LIMIT -> GTT trigger order, held on INDmoney's servers
            kind, body["order_type"], body["trigger_price"] = "gtt", "TRIGGER", order.trigger_price
            if order.limit_price is not None:
                body["trigger_limit_price"] = order.limit_price

        bids, asks = [], []
        try:
            bids, asks = parse_depth(self.client.market_depth([code]).get(code))
        except Exception as e:
            log.warning("market depth for %s failed: %s", inst.symbol, e)
        levels = asks if order.side is Side.BUY else bids
        sent_at = datetime.now()
        t0 = time.perf_counter()
        try:
            data = (self.client.place_order if kind == "order" else self.client.place_smart_order)(body)
            error = ""
        except Exception as e:
            data, error = {}, str(e)
        latency_ms = round((time.perf_counter() - t0) * 1000)

        ids, status = [], ""
        if kind == "order" and data.get("order_id"):
            ids, status = [data["order_id"]], data.get("order_status", "")
        for leg in data.get("order_data") or []:
            ids.append(leg.get("order_id"))
            status = leg.get("order_status", "")
            child = (leg.get("child_order_details") or {}).get("order_id")
            if child:
                ids.append(child)
        m = Mirrored(order.id, order.tag, inst.symbol, order.side.value, order.quantity, kind,
                     sent_at.isoformat(timespec="milliseconds"), [i for i in ids if i],
                     book_price=book_price(levels, order.quantity))
        m.closed = not m.broker_ids  # refused outright: nothing to follow up
        self.orders[order.id] = m
        log.info("LIVE %s %s %s %d -> %s %s (%d ms)%s", kind, order.side.value, inst.symbol, order.quantity,
                 ",".join(m.broker_ids) or "-", status, latency_ms, f" ERROR {error}" if error else "")
        self._csv("orders.csv", {
            "sent_at": m.sent_at, "sim_id": order.id, "tag": order.tag, "symbol": inst.symbol, "code": code,
            "kind": kind, "side": order.side.value, "qty": order.quantity, "order_type": body["order_type"],
            "trigger": order.trigger_price, "limit": order.limit_price,
            "best_bid": bids[0][0] if bids else None, "bid_qty": bids[0][1] if bids else None,
            "best_ask": asks[0][0] if asks else None, "ask_qty": asks[0][1] if asks else None,
            "book_price_for_qty": round(m.book_price, 2) if m.book_price else None,
            "depth": json.dumps({"bids": bids, "asks": asks}),
            "latency_ms": latency_ms, "broker_ids": " ".join(m.broker_ids), "status": status, "error": error[:300],
        })

    def _cancel(self, m: Mirrored, reason: str) -> None:
        for bid in m.broker_ids:
            if m.statuses.get(bid) in CLOSED_STATES:
                continue
            try:
                if bid.startswith("GTT-") or m.kind == "gtt":
                    self.client.cancel_smart_order(bid)
                else:
                    self.client.cancel_order(bid)
                log.info("LIVE cancelled %s (%s, %s)", bid, m.tag, reason)
            except Exception as e:  # usually already rejected / closed on INDmoney's side
                log.info("LIVE cancel %s (%s) not done: %s", bid, reason, e)
        m.closed = True

    def _unexpected_fill(self, m: Mirrored, row: dict) -> None:
        """An order filled although the account should be empty: flatten it and stop for the day."""
        qty = int(_num(row.get("traded_qty")) or 0)
        self.halted = f"order {row.get('id')} ({m.tag}) FILLED {qty} on INDmoney -- mirroring stopped"
        log.critical(self.halted)
        for other in self.orders.values():
            if other is not m and not other.closed:
                self._cancel(other, reason="unexpected fill")
        code = self.codes.get(m.symbol)
        if qty > 0 and code:
            exchange_seg, security_id = code.split("_", 1)
            body = {"txn_type": "SELL" if m.side == "BUY" else "BUY", "exchange": "BSE" if exchange_seg == "BFO" else "NSE",
                    "segment": SEGMENT, "product": PRODUCT, "validity": "DAY", "order_type": "MARKET",
                    "security_id": security_id, "qty": qty, "remarks": f"vif-{self.day:%m%d}-flatten-{m.sim_id}"}
            try:
                log.critical("LIVE flattening: %s", self.client.place_order(body))
            except Exception as e:
                log.critical("LIVE flattening order FAILED, close %s %d by hand: %s", m.symbol, qty, e)

    # ---- records ---------------------------------------------------------

    def _event(self, m: Mirrored, broker_id: str, row: dict) -> None:
        log.info("LIVE %s %s (%s): %s %s", broker_id, m.symbol, m.tag, row.get("status"), row.get("extra_info") or "")
        self._csv("events.csv", {
            "seen_at": datetime.now().isoformat(timespec="seconds"), "sim_id": m.sim_id, "tag": m.tag,
            "symbol": m.symbol, "broker_id": broker_id, "status": row.get("status"),
            "extra_info": row.get("extra_info"), "requested_price": row.get("requested_price"),
            "traded_price": row.get("traded_price"), "traded_qty": row.get("traded_qty"),
            "sl_trigger_price": row.get("sl_trigger_price"), "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at")})

    def _compare(self, fill: Fill) -> None:
        m = self.orders.get(fill.order_id)
        if m is None or m.book_price is None:
            return
        adverse = (m.book_price - fill.price) if fill.side is Side.BUY else (fill.price - m.book_price)
        self._csv("compare.csv", {
            "ts": fill.ts, "tag": fill.tag, "symbol": fill.instrument.symbol, "side": fill.side.value,
            "qty": fill.quantity, "paper_fill": round(fill.price, 2), "book_price_at_send": round(m.book_price, 2),
            "worse_by_per_unit": round(adverse, 2), "worse_by_rupees": round(adverse * fill.quantity, 2)})

    def _csv(self, name: str, row: dict) -> None:
        path = self.log_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "orders": [asdict(m) for m in self.orders.values()], "seen": sorted(self.seen),
            "fills_seen": self.fills_seen, "halted": self.halted}, indent=1))

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        s = json.loads(self.state_path.read_text())
        self.orders = {d["sim_id"]: Mirrored(**d) for d in s["orders"]}
        self.seen, self.fills_seen, self.halted = set(s["seen"]), s["fills_seen"], s["halted"]
