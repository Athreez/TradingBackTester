from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from backtester.run import run_backtest
from data.store import DataStore
from strategies.vix_iron_fly.strategy import VixIronFly

DAY = "2025-09-08"  # Monday -> NIFTY
EXPIRY = date(2025, 9, 9)
# Fixed 50% SL-M, +/-400 hedges, no VIX gating or hedge liquidity check, so these tests don't depend on config.yaml defaults.
BASE = {"vix_regimes": [], "stop_loss_pct": 50, "skip_expiry_day": False, "vix_min": None, "vix_max": None,
        "hedge_liquidity": {"min_lots": 0}, "stop_order": {"type": "market"},
        "schedule": {"NIFTY": {"hedge_distance": 400, "lots": 1}, "SENSEX": {"hedge_distance": 1300, "lots": 1}}}
SYMS = {
    ("CE", 24000): "NIFTY25SEP0924000CE", ("PE", 24000): "NIFTY25SEP0924000PE",
    ("CE", 24400): "NIFTY25SEP0924400CE", ("PE", 23600): "NIFTY25SEP0923600PE",
    ("CE", 24100): "NIFTY25SEP0924100CE",  # extra strike, never traded
}


def _day(price_at) -> pd.DataFrame:
    """Full 09:15-15:29 session; price_at(ts) -> (open, high, low, close)."""
    idx = pd.date_range(f"{DAY} 09:15", f"{DAY} 15:29", freq="1min", name="ts")
    rows = [price_at(ts) for ts in idx]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def flat(p):
    return lambda ts: (p, p, p, p)


@pytest.fixture
def store(tmp_path):
    s = DataStore(tmp_path)
    s.save_candles("1minute", "NSE", "NIFTY", _day(flat(24010)))

    def short_ce(ts):  # 100 until 10:30, spikes to 160 high in the 10:30 bar, then 155
        if ts < pd.Timestamp(f"{DAY} 10:30"):
            return (100, 100, 100, 100)
        if ts == pd.Timestamp(f"{DAY} 10:30"):
            return (110, 160, 105, 155)
        return (155, 155, 155, 155)

    def short_pe(ts):  # decays from 90 to 60 after noon
        return (90,) * 4 if ts < pd.Timestamp(f"{DAY} 12:00") else (60,) * 4

    prices = {("CE", 24000): short_ce, ("PE", 24000): short_pe,
              ("CE", 24400): flat(20), ("PE", 23600): flat(15), ("CE", 24100): flat(70)}
    for key, fn in prices.items():
        s.save_candles("1minute", "NFO", SYMS[key], _day(fn))
    s.save_contracts("NIFTY", EXPIRY, pd.DataFrame([
        {"trading_symbol": sym, "instrument_type": "OPTIDX", "strike_price": float(k),
         "option_type": ot, "lot_size": 75, "expiry": EXPIRY.isoformat()}
        for (ot, k), sym in SYMS.items()
    ]))
    return s


def test_exit_side_on_stop_loss(store):
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={**BASE, "on_stop_loss": "exit_side"})
    fills = {(f.tag, f.instrument.symbol): f for f in result.fills}
    at = lambda hm: datetime.fromisoformat(f"{DAY} {hm}")

    # Entry at 09:22, hedges +/-400 around ATM 24000 (spot 24010).
    for tag, sym in [("hedge:CE", "NIFTY25SEP0924400CE"), ("hedge:PE", "NIFTY25SEP0923600PE"),
                     ("short:CE", "NIFTY25SEP0924000CE"), ("short:PE", "NIFTY25SEP0924000PE")]:
        assert fills[(tag, sym)].ts == at("09:22")

    short_ce = fills[("short:CE", "NIFTY25SEP0924000CE")].price  # 100 less slippage
    sl = fills[("sl:CE", "NIFTY25SEP0924000CE")]
    assert sl.ts == at("10:30")
    assert sl.price == pytest.approx(round(short_ce * 1.5, 2) * 1.0005)  # SL-M at trigger + slippage

    # CE hedge sold on the next bar; PE side runs to 14:45.
    assert fills[("hedge-exit:CE", "NIFTY25SEP0924400CE")].ts == at("10:31")
    assert fills[("exit-time", "NIFTY25SEP0924000PE")].ts == at("14:45")
    assert fills[("exit-time", "NIFTY25SEP0923600PE")].ts == at("14:45")
    assert len(result.fills) == 8
    assert result.broker.positions() == {}
    assert ("sl:PE", "NIFTY25SEP0924000PE") not in fills


@pytest.mark.parametrize("pct, pe_stopped", [(0, True), (10, False)])
def test_other_stop_moves_after_a_stop_loss(store, pct, pe_stopped):
    """PE short sold at ~90 stays at 90: a stop at entry fires, one 10% above it does not."""
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={**BASE, "on_stop_loss": "exit_side", "other_stop_after_sl_pct": pct})
    fills = {(f.tag, f.instrument.symbol): f for f in result.fills}
    short_pe = fills[("short:PE", "NIFTY25SEP0924000PE")].price
    if pe_stopped:
        sl = fills[("sl:PE", "NIFTY25SEP0924000PE")]
        assert sl.ts == datetime.fromisoformat(f"{DAY} 10:31")
        assert short_pe < 90 and sl.price == pytest.approx(90 * 1.0005)  # already above the trigger: fills at the open
    else:
        assert ("exit-time", "NIFTY25SEP0924000PE") in fills
    assert result.broker.positions() == {} and result.broker.open_orders() == []


def test_exit_all_on_stop_loss(store):
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={**BASE, "on_stop_loss": "exit_all"})
    fills = {(f.tag, f.instrument.symbol): f for f in result.fills}
    at = lambda hm: datetime.fromisoformat(f"{DAY} {hm}")

    assert fills[("sl:CE", "NIFTY25SEP0924000CE")].ts == at("10:30")
    # Every remaining leg closes on the next bar; nothing waits for 14:45.
    for sym in ("NIFTY25SEP0924000PE", "NIFTY25SEP0924400CE", "NIFTY25SEP0923600PE"):
        assert fills[("sl-exit", sym)].ts == at("10:31")
    assert not any(tag in ("exit-time", "sl:PE") for tag, _ in fills)
    assert len(result.fills) == 8
    assert result.broker.positions() == {} and result.broker.open_orders() == []


@pytest.mark.parametrize("skip, expected_fills", [(True, 0), (False, 8)])
def test_skip_expiry_day(store, skip, expected_fills):
    # Make the trading day itself the nearest expiry.
    store.save_contracts("NIFTY", date(2025, 9, 8), store.load_contracts("NIFTY", EXPIRY))
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={**BASE, "skip_expiry_day": skip, "on_stop_loss": "exit_side"})
    assert len(result.fills) == expected_fills


def test_paused_skips_the_entry(store, monkeypatch):
    monkeypatch.setattr(VixIronFly, "paused", True, raising=False)  # what Telegram /pause sets on the instance
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store, overrides=BASE)
    assert result.fills == []


@pytest.mark.parametrize("max_dte, expected_fills", [(None, 8), (1, 8), (0, 0)])
def test_max_days_to_expiry(store, max_dte, expected_fills):
    # Mon 8 Sep, expiry Tue 9 Sep: 1 day away.
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={**BASE, "max_days_to_expiry": max_dte})
    assert len(result.fills) == expected_fills


@pytest.mark.parametrize("vix_min, vix_max, expected_fills", [
    (14, None, 8), (16, None, 0), (None, 14, 0), (None, 16, 8), (14, 16, 8),
])
def test_vix_filter(store, vix_min, vix_max, expected_fills):
    store.save_candles("1minute", "NSE", "INDIAVIX", _day(flat(15.0)))
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={**BASE, "vix_min": vix_min, "vix_max": vix_max, "on_stop_loss": "exit_side"})
    assert len(result.fills) == expected_fills


def test_vix_filter_without_vix_data_skips_day(store):
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store, overrides={**BASE, "vix_min": 10})
    assert result.fills == []


@pytest.mark.parametrize("vix, expected_sl_pct", [(10.0, 0.40), (15.0, 0.30), (12.0, None)])
def test_vix_regimes(store, vix, expected_sl_pct):
    store.save_candles("1minute", "NSE", "INDIAVIX", _day(flat(vix)))
    regimes = [{"vix_below": 11, "stop_loss_pct": 40}, {"vix_above": 14, "stop_loss_pct": 30}]
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={**BASE, "vix_regimes": regimes, "on_stop_loss": "exit_side"})
    if expected_sl_pct is None:
        assert result.fills == []
        return
    fills = {(f.tag, f.instrument.symbol): f for f in result.fills}
    short_ce = fills[("short:CE", "NIFTY25SEP0924000CE")].price
    sl = fills[("sl:CE", "NIFTY25SEP0924000CE")]
    assert sl.price == pytest.approx(round(short_ce * (1 + expected_sl_pct), 2) * 1.0005)


@pytest.mark.parametrize("nifty, sensex, expected", [
    ([date(2025, 9, 9)], [date(2025, 9, 11)], "NIFTY"),     # Mon after Sep 2025: NIFTY Tue < SENSEX Thu
    ([date(2025, 4, 10)], [date(2025, 4, 8)], "SENSEX"),    # before: SENSEX Tue < NIFTY Thu
    ([date(2025, 9, 9)], [date(2025, 9, 9)], "NIFTY"),      # tie -> first listed
    ([date(2025, 9, 5), date(2025, 9, 9)], [], "NIFTY"),    # past expiries ignored; no SENSEX data
    ([], [], None),
])
def test_trades_underlying_with_soonest_expiry(nifty, sensex, expected):
    expiries = {"NIFTY": nifty, "SENSEX": sensex}
    ctx = SimpleNamespace(data=SimpleNamespace(expiries=lambda u: expiries[u]))
    st = VixIronFly({**BASE, "entry_time": "09:22", "exit_time": "14:45",
                     "schedule": {"NIFTY": {"hedge_distance": 400, "lots": 1},
                                  "SENSEX": {"hedge_distance": 1300, "lots": 1}}})
    picked = st._pick_underlying(ctx, date(2025, 9, 8) if expected != "SENSEX" else date(2025, 4, 7))
    assert (picked[0] if picked else None) == expected


@pytest.mark.parametrize("underlying, cfg, spot, expected", [
    ("NIFTY", {"hedge_pct": 1.7}, 24010, 400),        # 408 -> nearest 50
    ("SENSEX", {"hedge_pct": 2.0}, 77254.71, 1500),   # 1545 -> nearest 100
    ("NIFTY", {"hedge_pct": 0.01}, 24010, 50),        # never below one strike step
    ("NIFTY", {"hedge_distance": 400}, 24010, 400),   # fixed points still supported
])
def test_hedge_distance(underlying, cfg, spot, expected):
    assert VixIronFly._hedge_distance(underlying, cfg, spot) == expected


LIQUID = {"hedge_liquidity": {"lookback_minutes": 5, "min_lots": 10},
          "schedule": {"NIFTY": {"hedge_distance": 400, "round_strike": 500, "lots": 1}}}


@pytest.mark.parametrize("ce_400_volume, ce_500_volume, expected_hedge_ce", [
    (1000.0, 1000.0, "NIFTY25SEP0924400CE"),   # target strike liquid -> used
    (1.0, 1000.0, "NIFTY25SEP0924500CE"),      # target thin -> nearest round strike (500s)
    (1.0, 1.0, None),                          # round strike thin too -> no trade
])
def test_illiquid_hedge_falls_back_to_round_strike(store, ce_400_volume, ce_500_volume, expected_hedge_ce):
    for sym, v in [("NIFTY25SEP0924400CE", ce_400_volume), ("NIFTY25SEP0924500CE", ce_500_volume),
                   ("NIFTY25SEP0923600PE", 1000.0)]:
        store.save_candles("1minute", "NFO", sym, _day(flat(20)).assign(volume=v))
    contracts = store.load_contracts("NIFTY", EXPIRY)
    extra = contracts.iloc[[0]].assign(trading_symbol="NIFTY25SEP0924500CE", strike_price=24500.0, option_type="CE")
    store.save_contracts("NIFTY", EXPIRY, pd.concat([contracts, extra], ignore_index=True))

    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={**BASE, **LIQUID, "on_stop_loss": "exit_side"})
    hedges = [f.instrument.symbol for f in result.fills if f.tag == "hedge:CE"]
    assert hedges == ([expected_hedge_ce] if expected_hedge_ce else [])
    if expected_hedge_ce is None:
        assert result.fills == []


def test_gtt_stop_limit_chases_at_market_when_price_gaps_past_limit(store):
    def short_ce(ts):  # 100 until 10:30, then gaps to 200 and stays there (past the 50% SL's limit)
        return (100,) * 4 if ts < pd.Timestamp(f"{DAY} 10:30") else (200,) * 4
    store.save_candles("1minute", "NFO", SYMS[("CE", 24000)], _day(short_ce))
    result = run_backtest("vix_iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={**BASE, "on_stop_loss": "exit_side",
                                     "stop_order": {"type": "limit", "limit_buffer_pct": 5, "chase_after_minutes": 1}})
    fills = {(f.tag, f.instrument.symbol): f for f in result.fills}
    at = lambda hm: datetime.fromisoformat(f"{DAY} {hm}")
    sl = fills[("sl:CE", "NIFTY25SEP0924000CE")]
    assert sl.ts == at("10:31") and sl.price == pytest.approx(200 * 1.0005)  # market buy, a bar after triggering
    assert fills[("hedge-exit:CE", "NIFTY25SEP0924400CE")].ts == at("10:32")
    assert len(result.fills) == 8 and result.broker.positions() == {}
