from datetime import date, datetime

import pandas as pd
import pytest

from backtester.run import run_backtest
from data.store import DataStore

DAY = "2025-09-08"  # Monday -> NIFTY
EXPIRY = date(2025, 9, 9)
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
    result = run_backtest("iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={"on_stop_loss": "exit_side"})
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


def test_exit_all_on_stop_loss(store):
    result = run_backtest("iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={"on_stop_loss": "exit_all"})
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
    result = run_backtest("iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={"skip_expiry_day": skip, "on_stop_loss": "exit_side"})
    assert len(result.fills) == expected_fills


@pytest.mark.parametrize("vix_min, vix_max, expected_fills", [
    (14, None, 8), (16, None, 0), (None, 14, 0), (None, 16, 8), (14, 16, 8),
])
def test_vix_filter(store, vix_min, vix_max, expected_fills):
    store.save_candles("1minute", "NSE", "INDIAVIX", _day(flat(15.0)))
    result = run_backtest("iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store,
                          overrides={"vix_min": vix_min, "vix_max": vix_max, "on_stop_loss": "exit_side"})
    assert len(result.fills) == expected_fills


def test_vix_filter_without_vix_data_skips_day(store):
    result = run_backtest("iron_fly", date(2025, 9, 8), date(2025, 9, 8), store=store, overrides={"vix_min": 10})
    assert result.fills == []
