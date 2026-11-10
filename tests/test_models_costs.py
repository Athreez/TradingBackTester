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


@pytest.mark.parametrize("exchange, side, qty, price, api_total", [
    # total_charges from INDmoney's own /margin endpoint (2026-11-06) for API option orders
    (Exchange.NFO, Side.SELL, 65, 100, 24.25), (Exchange.NFO, Side.BUY, 65, 100, 14.69),
    (Exchange.BFO, Side.SELL, 20, 100, 15.63), (Exchange.BFO, Side.BUY, 20, 100, 12.69),
    (Exchange.NFO, Side.SELL, 650, 1000, 1259.49), (Exchange.NFO, Side.BUY, 650, 1000, 303.99),
    (Exchange.BFO, Side.SELL, 200, 1000, 395.70), (Exchange.BFO, Side.BUY, 200, 1000, 101.70),
])
def test_default_costs_match_indmoney_api(exchange, side, qty, price, api_total):
    assert CostModel().charges(exchange, side, qty, price) == pytest.approx(api_total, abs=0.05)


def test_cost_presets_from_config():
    assert CostModel.from_dict(None).brokerage_per_order == 10
    assert CostModel.from_dict({"preset": "zerodha", "slippage": 0.001}).brokerage_per_order == 20
    with pytest.raises(ValueError):
        CostModel.from_dict({"preset": "upstox"})


def test_zerodha_nifty_round_trip():
    # 1 lot (75) NIFTY option: sell @ 100, buy back @ 80.
    c = CostModel.zerodha()
    sell = c.charges(Exchange.NFO, Side.SELL, 75, 100.0)  # turnover 7500
    buy = c.charges(Exchange.NFO, Side.BUY, 75, 80.0)  # turnover 6000
    exp_sell = 20 + 7500 * (0.0003553 + 1e-9 + 1e-6) + 7500 * 0.0015 \
        + (20 + 7500 * (0.0003553 + 1e-6 + 1e-9)) * 0.18
    exp_buy = 20 + 6000 * (0.0003553 + 1e-9 + 1e-6) + 6000 * 0.00003 \
        + (20 + 6000 * (0.0003553 + 1e-6 + 1e-9)) * 0.18
    assert sell == pytest.approx(exp_sell) and buy == pytest.approx(exp_buy)
    assert sell + buy == pytest.approx(64.31, abs=0.01)


def test_settlement_charges():
    c = CostModel()  # Rs 10 brokerage + 18% GST
    assert c.settlement_charges(was_long=True, quantity=75, intrinsic=0) == 0
    assert c.settlement_charges(was_long=True, quantity=75, intrinsic=50) == pytest.approx(11.8 + 3750 * 0.0015)
    assert c.settlement_charges(was_long=False, quantity=75, intrinsic=50) == pytest.approx(11.8)


def test_costs_sell_has_stt_buy_has_stamp():
    c = CostModel(brokerage_per_order=0, gst=0, sebi_fee=0, exchange_txn={Exchange.NFO: 0}, ipft={})
    assert c.charges(Exchange.NFO, Side.SELL, 100, 100.0) == pytest.approx(10_000 * c.stt_sell)
    assert c.charges(Exchange.NFO, Side.BUY, 100, 100.0) == pytest.approx(10_000 * c.stamp_duty_buy)


def test_rates_follow_the_trade_date():
    from datetime import date
    c = CostModel()
    stt = lambda day: c.charges(Exchange.NFO, Side.SELL, 100, 100.0, day) - c.charges(Exchange.NFO, Side.BUY, 100, 100.0, day)
    assert c.on(date(2026, 4, 1)).stt_sell == c.on(None).stt_sell == 0.0015
    assert c.on(date(2026, 3, 31)).stt_sell == 0.001
    assert c.on(date(2024, 9, 30)).stt_sell == 0.000625 and c.on(date(2024, 9, 30)).txn_rate(Exchange.NFO) == 0.000495
    assert c.on(date(2022, 1, 3)).txn_rate(Exchange.NFO) == 0.00053
    assert stt(date(2022, 1, 3)) == pytest.approx(10_000 * (0.0005 - 0.00003))
    assert CostModel.from_dict({"history": []}).on(date(2022, 1, 3)).stt_sell == 0.0015


def test_cost_model_from_config():
    c = CostModel.from_dict({"brokerage_per_order": 10, "exchange_txn": {"BFO": 0.0005}})
    assert c.brokerage_per_order == 10 and c.txn_rate(Exchange.BFO) == 0.0005


def test_lot_sizes_by_expiry():
    from datetime import date
    from data.underlyings import get_underlying
    nifty, sensex = get_underlying("NIFTY"), get_underlying("SENSEX")
    assert nifty.lot_size(date(2022, 6, 2)) == 50
    assert nifty.lot_size(date(2024, 12, 26)) == 25 and nifty.lot_size(date(2025, 1, 2)) == 75
    assert sensex.lot_size(date(2025, 1, 3)) == 10 and sensex.lot_size(date(2025, 1, 7)) == 20
    assert nifty.lot_size(date(2025, 12, 23)) == 75
    assert nifty.lot_size(date(2026, 1, 6)) == 65
    assert sensex.lot_size(date(2025, 9, 4)) == 20
    assert sensex.lot_size(date(2027, 1, 28)) == 25
