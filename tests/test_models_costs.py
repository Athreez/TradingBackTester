import pytest

from core.costs import CostModel
from core.models import Exchange, Instrument, Position, Side

INST = Instrument("NIFTYTEST", Exchange.NFO, "NIFTY", lot_size=75)


def test_position_open_add_reduce_close():
    p = Position(INST)
    p.apply(Side.SELL, 75, 100.0)
    p.apply(Side.SELL, 75, 110.0)
    assert p.quantity == -150 and p.avg_price == pytest.approx(105.0)
    p.apply(Side.BUY, 75, 90.0)
    assert p.quantity == -75 and p.realized_pnl == pytest.approx(75 * 15)
    p.apply(Side.BUY, 75, 105.0)
    assert p.quantity == 0 and p.realized_pnl == pytest.approx(75 * 15)


def test_position_flip():
    p = Position(INST)
    p.apply(Side.BUY, 75, 100.0)
    p.apply(Side.SELL, 150, 120.0)
    assert p.quantity == -75 and p.avg_price == 120.0 and p.realized_pnl == pytest.approx(75 * 20)


def test_costs_sell_has_stt_buy_has_stamp():
    c = CostModel(brokerage_per_order=0, gst=0, sebi_fee=0, exchange_txn={Exchange.NFO: 0})
    assert c.charges(Exchange.NFO, Side.SELL, 100, 100.0) == pytest.approx(10_000 * c.stt_sell)
    assert c.charges(Exchange.NFO, Side.BUY, 100, 100.0) == pytest.approx(10_000 * c.stamp_duty_buy)


def test_cost_model_from_config():
    c = CostModel.from_dict({"brokerage_per_order": 10, "exchange_txn": {"BFO": 0.0005}})
    assert c.brokerage_per_order == 10 and c.txn_rate(Exchange.BFO) == 0.0005
