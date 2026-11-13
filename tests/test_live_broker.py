"""LiveBroker against a fake INDmoney: nothing here talks to the real API."""
import pickle
from datetime import date, datetime, timedelta

import pytest
import requests

from backtester.engine import run_bar
from backtester.historical_data import HistoricalMarketData
from core.costs import CostModel
from core.models import Exchange, Instrument, Order, OrderStatus, OrderType, Side
from core.registry import load_strategy
from core.strategy_base import Context
from data.indmoney_client import IndMoneyError
from live_trader.broker import LiveBroker
from live_trader.runner import LiveBot
from paper_trader.runner import save_state
from test_vix_iron_fly import BASE, DAY, SYMS, store  # noqa: F401 (store is a fixture)

T0 = datetime(2025, 9, 8, 9, 30)


class FakeIndMoney:
    """Order book, GTTs and positions the way INDmoney reports them (numbers as strings)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.price = lambda sid: 100.0  # security id -> market price
        self.fill_market = True  # market orders trade at once
        self.reject = set()  # security ids whose orders FAIL
        self.raise_on_place = None  # exception for the next place call
        self.hide_triggered = False  # a fired GTT's order never shows in the order book (positions only)
        self.refuse_gtt = False
        self._n = 0

    def _row(self, prefix, body, status):
        self._n += 1
        rid = f"{prefix}-{self._n}"
        self.rows[rid] = {"id": rid, "status": status, "traded_qty": 0, "traded_price": "", "extra_info": "",
                          "requested_qty": body["qty"], "remarks": body.get("remarks"), "txn_type": body["txn_type"],
                          "security_id": body["security_id"], "trigger": body.get("trigger_price"),
                          "limit": body.get("trigger_limit_price")}
        return rid

    def _trade(self, rid, price):
        r = self.rows[rid]
        r.update(status="SUCCESS", traded_qty=r["requested_qty"], traded_price=f"{price:.2f}")

    def place_order(self, body):
        self.calls.append(("order", body))
        if self.raise_on_place:
            e, self.raise_on_place = self.raise_on_place, None
            if isinstance(e, tuple):  # (exception, place it anyway)
                rid = self._row("DRV", body, "PENDING")
                self._trade(rid, self.price(body["security_id"]))
                raise e[0]
            raise e
        rid = self._row("DRV", body, "PENDING")
        if body["security_id"] in self.reject:
            self.rows[rid].update(status="FAILED", extra_info="Insufficient margin")
        elif self.fill_market:
            self._trade(rid, self.price(body["security_id"]))
        return {"order_id": rid, "order_status": "INITIATED"}

    def place_smart_order(self, body):
        self.calls.append(("gtt", body))
        if self.refuse_gtt:
            raise IndMoneyError("POST /smart/order failed: HTTP 400: trigger price must be greater than CMP")
        rid = self._row("GTT", body, "PENDING")
        return {"order_data": [{"order_id": rid, "order_status": "CREATED"}]}

    def fire(self, gtt_id, price):
        """The GTT's trigger trades: INDmoney creates an order carrying the GTT's remark."""
        g = self.rows[gtt_id]
        g["status"] = "SUCCESS"
        body = {"qty": g["requested_qty"], "remarks": g["remarks"], "txn_type": g["txn_type"],
                "security_id": g["security_id"]}
        rid = self._row("DRV", body, "PENDING")
        self._trade(rid, price)
        if self.hide_triggered:
            self.rows[rid]["remarks"] = None
            self.rows[rid]["hidden"] = True
        return rid

    def cancel_order(self, rid):
        self.calls.append(("cancel", {"id": rid}))
        if self.rows[rid]["status"] not in ("SUCCESS", "FAILED"):
            self.rows[rid]["status"] = "CANCELLED"

    cancel_smart_order = cancel_order

    def order_book(self):
        return [dict(r) for r in self.rows.values() if not r.get("hidden")]

    def positions(self):
        out = {}
        for r in self.rows.values():
            if r["traded_qty"]:
                p = out.setdefault(r["security_id"], {"security_id": r["security_id"], "product": "INTRADAY",
                                                      "net_qty": 0, "day_buy_qty": 0, "day_buy_val": 0.0,
                                                      "day_sell_qty": 0, "day_sell_val": 0.0})
                side = "buy" if r["txn_type"] == "BUY" else "sell"
                p["net_qty"] += r["traded_qty"] if side == "buy" else -r["traded_qty"]
                p[f"day_{side}_qty"] += r["traded_qty"]
                p[f"day_{side}_val"] += r["traded_qty"] * float(r["traded_price"])
        return [{k: str(v) for k, v in p.items()} for p in out.values()]

    def sent(self, kind=None):
        return [b for k, b in self.calls if k != "cancel" and (kind is None or k == kind)]


INST = Instrument("NIFTY25SEP0924000CE", Exchange.NFO, "NIFTY", 75, date(2025, 9, 9), 24000.0)
HEDGE = Instrument("NIFTY25SEP0924400CE", Exchange.NFO, "NIFTY", 75, date(2025, 9, 9), 24400.0)


@pytest.fixture
def setup(tmp_path):
    fake, clock = FakeIndMoney(), {"now": T0}
    codes = {INST.symbol: "NFO_1", HEDGE.symbol: "NFO_2"}
    b = LiveBroker(fake, codes, CostModel(), 500_000, tmp_path, now=lambda: clock["now"])
    b.start_day(T0.date())
    return b, fake, clock


def order(inst, side, qty=75, tag="", **kw):
    return Order(inst, side, qty, tag=tag, **kw)


def tick(b, clock, seconds=5):
    clock["now"] += timedelta(seconds=seconds)
    b.poll()
    b.send_pending()


def test_shorts_wait_for_the_hedges(setup):
    b, fake, clock = setup
    fake.fill_market = False
    b.place_order(order(HEDGE, Side.BUY, tag="hedge:CE"))
    b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    assert [x["security_id"] for x in fake.sent()] == ["2"]  # only the hedge went out
    fake._trade("DRV-1", 20.0)
    tick(b, clock)
    assert [(x["security_id"], x["txn_type"]) for x in fake.sent()] == [("2", "BUY"), ("1", "SELL")]
    assert [f.tag for f in b.take_fills()] == ["hedge:CE"]


def test_failed_hedge_means_no_short(setup):
    b, fake, clock = setup
    fake.reject = {"2"}
    b.place_order(order(HEDGE, Side.BUY, tag="hedge:CE"))
    short = b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    tick(b, clock)
    assert "Insufficient margin" in b.entry_failed and short.status is OrderStatus.REJECTED
    assert [x["security_id"] for x in fake.sent()] == ["2"]
    assert any("Insufficient margin" in a for a in b.alerts)


def test_stop_is_a_gtt_and_its_fill_is_found_by_remark(setup):
    b, fake, clock = setup
    b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    tick(b, clock)
    stop = b.place_order(order(INST, Side.BUY, tag="sl:CE", order_type=OrderType.STOP_LIMIT,
                               trigger_price=130.03, limit_price=136.5))
    b.send_pending()
    gtt = fake.sent("gtt")[0]
    assert gtt["order_type"] == "TRIGGER" and gtt["trigger_price"] == 130.05 and gtt["trigger_limit_price"] == 136.5
    assert b.open_orders() == [stop]
    fake.fire("GTT-2", 131.0)
    tick(b, clock)
    assert stop.status is OrderStatus.FILLED and b.positions() == {}
    assert [(f.tag, f.price) for f in b.take_fills()] == [("short:CE", 100.0), ("sl:CE", 131.0)]


def test_exit_waits_for_the_stop_cancel_and_never_buys_twice(setup):
    b, fake, clock = setup
    b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    tick(b, clock)
    stop = b.place_order(order(INST, Side.BUY, tag="sl:CE", order_type=OrderType.STOP, trigger_price=130))
    b.send_pending()
    # 14:45: the strategy cancels the stop and buys back -- but the GTT fires before the cancel lands
    fake.fire("GTT-2", 132.0)
    real_cancel = fake.cancel_smart_order
    fake.cancel_smart_order = lambda rid: fake.calls.append(("cancel", {"id": rid}))  # too late: already traded
    b.cancel_order(stop.id)
    exit_ = b.place_order(order(INST, Side.BUY, tag="exit-time"))
    b.send_pending()
    assert len(fake.sent("order")) == 1  # the exit waits for the cancel to resolve
    tick(b, clock)
    assert stop.status is OrderStatus.FILLED and exit_.status is OrderStatus.CANCELLED
    assert len(fake.sent("order")) == 1 and b.positions() == {}
    fake.cancel_smart_order = real_cancel


def test_tightened_stop_replaces_the_old_one(setup):
    b, fake, clock = setup
    b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    tick(b, clock)
    old = b.place_order(order(INST, Side.BUY, tag="sl:CE", order_type=OrderType.STOP, trigger_price=130))
    b.send_pending()
    b.cancel_order(old.id)
    b.place_order(order(INST, Side.BUY, tag="sl:CE", order_type=OrderType.STOP, trigger_price=110))
    b.send_pending()
    assert len(fake.sent("gtt")) == 1  # waits for the cancel
    tick(b, clock)
    assert [g["trigger_price"] for g in fake.sent("gtt")] == [130.0, 110.0] and old.status is OrderStatus.CANCELLED


def test_refused_gtt_closes_at_market(setup):
    b, fake, clock = setup
    b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    tick(b, clock)
    fake.refuse_gtt = True
    b.place_order(order(INST, Side.BUY, tag="sl:CE", order_type=OrderType.STOP, trigger_price=99))
    b.send_pending()
    tick(b, clock)
    assert fake.sent()[-1]["order_type"] == "MARKET" and b.positions() == {}
    assert any("refused" in a for a in b.alerts)


def test_timeout_is_looked_up_by_remark_not_resent(setup):
    b, fake, clock = setup
    fake.raise_on_place = (requests.Timeout("read timed out"), True)  # timed out, but INDmoney placed it
    o = b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    tick(b, clock)
    assert o.status is OrderStatus.FILLED and len(fake.sent()) == 1


def test_lost_closing_order_is_sent_again(setup):
    b, fake, clock = setup
    b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    tick(b, clock)
    fake.raise_on_place = requests.ConnectionError("reset")  # never reached INDmoney
    b.place_order(order(INST, Side.BUY, tag="exit-time"))
    b.send_pending()
    tick(b, clock, 40)  # not in the order book after 30s: sent again
    tick(b, clock)
    assert len(fake.sent()) == 3 and b.positions() == {}


def test_stuck_market_order_is_cancelled_and_resent(setup):
    b, fake, clock = setup
    fake.fill_market = False
    b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    tick(b, clock, 61)  # still open after a minute: cancel requested
    tick(b, clock)  # cancel confirmed: remainder sent again
    assert len(fake.sent()) == 2 and fake.rows["DRV-1"]["status"] == "CANCELLED"
    fake._trade("DRV-2", 99.0)
    tick(b, clock)
    assert b.positions()[INST].quantity == -75


def test_positions_book_a_fill_the_order_book_never_shows(setup):
    b, fake, clock = setup
    b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    tick(b, clock)
    b.place_order(order(INST, Side.BUY, tag="sl:CE", order_type=OrderType.STOP, trigger_price=130))
    b.send_pending()
    fake.hide_triggered = True
    fake.fire("GTT-2", 133.0)
    for _ in range(3):
        tick(b, clock, 31)
    assert b.positions() == {} and b.fills()[-1].price == 133.0 and b.halted is None


def test_unexplained_difference_halts_new_entries(setup):
    b, fake, clock = setup
    b.place_order(order(INST, Side.SELL, tag="short:CE"))
    b.send_pending()
    fake.rows["DRV-1"]["traded_qty"] = 150  # INDmoney shows more than we ever sent
    fake.rows["DRV-1"]["hidden"] = True
    for _ in range(4):
        tick(b, clock, 31)
    assert "positions differ" in b.halted
    assert b.place_order(order(HEDGE, Side.BUY, tag="hedge:CE")).status is OrderStatus.REJECTED
    assert any("positions differ" in a for a in b.alerts)


def test_refusals(setup):
    b, *_ = setup
    assert b.place_order(order(INST, Side.SELL, qty=150)).status is OrderStatus.REJECTED  # 2 lots > max 1
    b.accept_opening = False
    assert b.place_order(order(INST, Side.SELL)).status is OrderStatus.REJECTED  # catch-up bar


def test_state_pickles_without_the_connection(setup, tmp_path):
    b, *_ = setup
    b._quote = lambda i: None
    save_state(tmp_path / "s.pkl", "vix_iron_fly", b, None, None, None, False)
    state = pickle.loads((tmp_path / "s.pkl").read_bytes())["broker"]
    assert "client" not in state and "sent" in state


# ---- the strategy end to end, through LiveBot's sync step -------------------------------------------------

def run_live_day(store, tmp_path, reject=()):
    """A full session: strategy -> LiveBroker -> fake INDmoney, synced the way LiveBot syncs every poll."""
    data = HistoricalMarketData(store, "1minute")
    codes = {sym: f"NFO_{i}" for i, sym in enumerate(SYMS.values(), 100)}
    by_sid = {c.split("_")[1]: s for s, c in codes.items()}
    clock = {"now": datetime.fromisoformat(f"{DAY} 09:15")}
    fake = FakeIndMoney()
    fake.reject = {codes[sym].split("_")[1] for sym in reject}

    fake.price = lambda sid: float(_candles(store, by_sid[sid]).loc[:data.now()].iloc[-1]["close"])
    broker = LiveBroker(fake, codes, CostModel(), 500_000, tmp_path, now=lambda: clock["now"])
    broker.start_day(date.fromisoformat(DAY))
    broker._quote = lambda inst: data.bar_at(inst, data.now())
    strategy, _ = load_strategy("vix_iron_fly", {**BASE, "on_stop_loss": "exit_side"})
    bot = LiveBot.__new__(LiveBot)
    bot.broker, bot.strategy, bot.notifier = broker, strategy, None
    bot._log_new_fills = lambda **kw: None
    bot._save = lambda: None
    ctx = Context(data, broker)

    for ts in data.index_frame("NIFTY").index:
        ts = ts.to_pydatetime()
        clock["now"] = ts + timedelta(minutes=1, seconds=5)
        for gid, r in list(fake.rows.items()):  # GTTs fire on INDmoney's side when the price gets there
            if gid.startswith("GTT") and r["status"] == "PENDING":
                bar = _candles(store, by_sid[r["security_id"]]).loc[ts]
                if bar["high"] >= r["trigger"]:
                    fake.fire(gid, min(bar["close"], r["limit"]))
        run_bar(strategy, ctx, broker, data, ts)
        bot._sync_broker(ctx)
        bot._sync_broker(ctx)
    return broker, fake, strategy, codes


def test_iron_fly_day_through_the_live_broker(store, tmp_path):  # noqa: F811
    broker, fake, strategy, codes = run_live_day(store, tmp_path)
    first = [(b["txn_type"], b["security_id"]) for b in fake.sent()[:4]]
    assert first[:2] == [("BUY", codes[SYMS[("CE", 24400)]][4:]), ("BUY", codes[SYMS[("PE", 23600)]][4:])]
    assert {t for t, _ in first[2:]} == {"SELL"}
    gtts = fake.sent("gtt")
    assert len(gtts) == 2 and all(g["order_type"] == "TRIGGER" for g in gtts)
    tags = [f.tag for f in broker.fills()]
    assert tags.count("sl:CE") == 1 and "hedge-exit:CE" in tags and tags.count("exit-time") == 2
    assert broker.positions() == {} and broker.working() == [] and not broker.alerts
    assert {p["net_qty"] for p in fake.positions()} == {"0"}


def _candles(store, symbol):
    return store.load_candles("1minute", "NFO", symbol)


def test_rejected_hedge_closes_the_other_and_never_sells(store, tmp_path):  # noqa: F811
    broker, fake, strategy, codes = run_live_day(store, tmp_path, reject=[SYMS[("PE", 23600)]])
    assert not any(b["txn_type"] == "SELL" and b["security_id"] in {codes[SYMS[("CE", 24000)]][4:],
                                                                   codes[SYMS[("PE", 24000)]][4:]}
                   for b in fake.sent())
    assert [f.tag for f in broker.fills()] == ["hedge:CE", "entry-failed"]
    assert broker.positions() == {} and strategy.state == "DONE" and "entry abandoned" in strategy.note
