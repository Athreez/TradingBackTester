from datetime import datetime

import pandas as pd

from backtester.historical_data import HistoricalMarketData
from data.store import DataStore


def test_index_value_never_falls_back_to_a_stale_candle(tmp_path):
    """2026-11-03: INDmoney had no NIFTY candles, and spot() returned the previous day's close."""
    store = DataStore(tmp_path)
    idx = pd.date_range("2026-11-02 09:15", "2026-11-02 09:30", freq="1min", name="ts")
    store.save_candles("1minute", "NSE", "NIFTY", pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 0.0}, index=idx))
    data = HistoricalMarketData(store, "1minute")

    data.set_time(datetime(2026, 11, 2, 9, 30))
    assert data.spot("NIFTY") == 100.0
    data.set_time(datetime(2026, 11, 2, 9, 40))  # 10 min gap: still usable
    assert data.spot("NIFTY") == 100.0
    data.set_time(datetime(2026, 11, 2, 10, 0))  # 30 min without a candle: stale
    assert data.spot("NIFTY") is None
    data.set_time(datetime(2026, 11, 3, 9, 21))  # next day, no candles yet: yesterday's close is not spot
    assert data.spot("NIFTY") is None


def test_spot_falls_back_to_option_parity_when_index_is_missing(tmp_path):
    """2026-11-03: no NIFTY 50 candles at INDmoney; spot must come from the expiry's options."""
    from datetime import date
    store = DataStore(tmp_path)
    prev = pd.date_range("2026-11-02 15:20", "2026-11-02 15:29", freq="1min", name="ts")
    store.save_candles("1minute", "NSE", "NIFTY", pd.DataFrame(
        {"open": 23440.0, "high": 23440.0, "low": 23440.0, "close": 23440.0, "volume": 0.0}, index=prev))
    expiry, true_spot = date(2026, 11, 3), 23563.0  # gapped up 120 points overnight
    today = pd.date_range("2026-11-03 09:15", "2026-11-03 09:30", freq="1min", name="ts")
    rows = []
    for k in range(23300, 23850, 50):
        for t in ("CE", "PE"):
            intrinsic = max(0.0, true_spot - k) if t == "CE" else max(0.0, k - true_spot)
            px = intrinsic + 40.0  # same time value on both legs keeps put-call parity exact
            sym = f"NIFTY26NOV03{k}{t}"
            store.save_candles("1minute", "NFO", sym, pd.DataFrame(
                {"open": px, "high": px, "low": px, "close": px, "volume": 1.0}, index=today))
            rows.append({"trading_symbol": sym, "strike_price": float(k), "option_type": t, "expiry": expiry})
    store.save_contracts("NIFTY", expiry, pd.DataFrame(rows))

    data = HistoricalMarketData(store, "1minute")
    data.set_time(datetime(2026, 11, 3, 9, 21))
    assert data.index_value("NIFTY") is None
    assert abs(data.spot("NIFTY") - true_spot) < 2.0  # expiry day: only ~6h of carry (about 1 point)
