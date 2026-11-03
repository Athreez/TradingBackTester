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
