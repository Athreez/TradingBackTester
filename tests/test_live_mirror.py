from datetime import date, datetime

import pytest

from core.models import Exchange, Fill, Instrument, Order, OrderStatus, OrderType, Side
from live_trader.mirror import OrderMirror, available_balance, book_price, parse_depth

DAY = date(2026, 11, 11)
INST = Instrument("NIFTY26NOV1724500CE", Exchange.NFO, "NIFTY", lot_size=65)
DEPTH = {"market_depth": {"depth": [
    {"buy": {"quantity": "130", "price": "99.50"}, "sell": {"quantity": "40", "price": "100.00"}},
    {"buy": {"quantity": "1,300", "price": "99.00"}, "sell": {"quantity": "2,600", "price": "100.50"}},
]}}


class FakeClient:
    def __init__(self, balance=0.0, book=None):
        self.balance, self.book, self.calls = balance, book or [], []

    def funds(self):
        return {"sod_balance": 0, "withdrawal_balance": 0, "detailed_avl_balance": {"option_buy": self.balance}}

    def market_depth(self, codes):
        return {codes[0]: DEPTH}

    def place_order(self, body):
        self.calls.append(("order", body))
        return {"order_id": f"DRV-{len(self.calls)}", "order_status": "INITIATED"}

    def place_smart_order(self, body):
        self.calls.append(("gtt", body))
        return {"order_data": [{"order_id": "DRV-90", "order_status": "CREATED",
                                "child_order_details": {"order_id": "GTT-91", "order_status": "CREATED"}}]}

    def cancel_order(self, order_id, segment="DERIVATIVE"):
        self.calls.append(("cancel", order_id))

    def cancel_smart_order(self, order_id, segment="DERIVATIVE"):
        self.calls.append(("cancel_gtt", order_id))

    def order_book(self):
        return self.book


class FakeBroker:
    def __init__(self):
        self._orders, self._fills = [], []

    def orders(self):
        return self._orders

    def fills(self):
        return self._fills


def _mirror(tmp_path, client):
    return OrderMirror(client, {INST.symbol: "NFO_51011"}, DAY, tmp_path)


def test_parse_depth_and_book_price():
    bids, asks = parse_depth(DEPTH)
    assert bids[1] == (99.0, 1300.0) and asks[0] == (100.0, 40.0)
    assert book_price(asks, 65) == pytest.approx((40 * 100 + 25 * 100.5) / 65)
    assert book_price(asks, 10_000) is None  # book too thin for the quantity
    assert available_balance({"sod_balance": "0", "detailed_avl_balance": {"future": "1,250.5"}}) == 1250.5
    # The live API nests the book under the scrip code once more.
    live = {"market_depth": {"NFO_45314": {"depth": [{"buy": {"quantity": "130.00", "price": "1,166.75"},
                                                      "sell": {"quantity": "65.00", "price": "1,173.35"}}]}}}
    assert parse_depth(live) == ([(1166.75, 130.0)], [(1173.35, 65.0)])


def test_sends_live_orders_only_and_compares_fills(tmp_path):
    client, broker = FakeClient(), FakeBroker()
    m = _mirror(tmp_path, client)
    old = Order(INST, Side.SELL, 65, tag="short:CE")
    broker._orders.append(old)
    m.start(broker)  # orders already in the account are history

    catch_up = Order(INST, Side.SELL, 65, tag="short:CE")
    broker._orders.append(catch_up)
    m.sync(broker, datetime(2026, 11, 11, 9, 21), live=False)
    assert client.calls == []

    buy = Order(INST, Side.BUY, 65, tag="hedge:CE")
    stop = Order(INST, Side.BUY, 65, OrderType.STOP_LIMIT, limit_price=136.5, trigger_price=130.0, tag="sl:CE")
    broker._orders += [buy, stop]
    m.sync(broker, datetime(2026, 11, 11, 9, 22), live=True)
    (k1, b1), (k2, b2) = client.calls
    assert k1 == "order" and b1["order_type"] == "MARKET" and b1["txn_type"] == "BUY" and b1["exchange"] == "NSE"
    assert b1["security_id"] == "51011" and b1["qty"] == 65 and b1["product"] == "INTRADAY"
    assert k2 == "gtt" and b2["order_type"] == "TRIGGER" and b2["trigger_price"] == 130.0
    assert b2["trigger_limit_price"] == 136.5 and "limit_price" not in b2

    broker._fills.append(Fill(buy.id, INST, Side.BUY, 65, 100.2, datetime(2026, 11, 11, 9, 23)))
    m.sync(broker, datetime(2026, 11, 11, 9, 23), live=True)
    rows = (tmp_path / "logs" / "compare.csv").read_text().splitlines()
    assert rows[1].split(",")[5:7] == ["100.2", "100.19"]  # paper fill vs the book's price for 65

    stop.status = OrderStatus.CANCELLED
    m.sync(broker, datetime(2026, 11, 11, 9, 24), live=True)
    assert ("cancel_gtt", "DRV-90") in client.calls and ("cancel_gtt", "GTT-91") in client.calls


def test_refuses_when_the_account_has_money(tmp_path):
    client, broker = FakeClient(balance=50_000), FakeBroker()
    m = _mirror(tmp_path, client)
    m.start(broker)
    broker._orders.append(Order(INST, Side.BUY, 65, tag="hedge:CE"))
    m.sync(broker, datetime(2026, 11, 11, 9, 22), live=True)
    assert client.calls == [] and "available" in m.halted


def test_unexpected_fill_is_flattened_and_stops_mirroring(tmp_path):
    client, broker = FakeClient(), FakeBroker()
    m = _mirror(tmp_path, client)
    m.start(broker)
    broker._orders.append(Order(INST, Side.BUY, 65, tag="hedge:CE"))
    m.sync(broker, datetime(2026, 11, 11, 9, 22), live=True)
    client.book = [{"id": "DRV-1", "status": "SUCCESS", "traded_qty": 65, "traded_price": "100.00"}]
    m.poll()
    flatten = client.calls[-1]
    assert flatten[0] == "order" and flatten[1]["txn_type"] == "SELL" and flatten[1]["qty"] == 65
    assert "FILLED" in m.halted
    broker._orders.append(Order(INST, Side.SELL, 65, tag="short:CE"))
    m.sync(broker, datetime(2026, 11, 11, 9, 23), live=True)
    assert len(client.calls) == 2  # nothing more sent


def test_rejection_closes_the_order_and_restart_does_not_resend(tmp_path):
    client, broker = FakeClient(), FakeBroker()
    m = _mirror(tmp_path, client)
    m.start(broker)
    broker._orders.append(Order(INST, Side.BUY, 65, tag="hedge:CE"))
    m.sync(broker, datetime(2026, 11, 11, 9, 22), live=True)
    client.book = [{"id": "DRV-1", "status": "FAILED", "extra_info": "Insufficient margin", "traded_qty": 0}]
    m.poll()
    assert m.orders[broker._orders[0].id].closed
    assert "Insufficient margin" in (tmp_path / "logs" / "events.csv").read_text()

    again = _mirror(tmp_path, client)  # same day, after a restart
    again.start(broker)
    again.sync(broker, datetime(2026, 11, 11, 9, 23), live=True)
    assert len(client.calls) == 1
