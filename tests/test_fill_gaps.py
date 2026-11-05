from datetime import date

import pandas as pd
import pytest

import data.downloader as dl
from data.store import DataStore

DAY1, DAY2 = date(2026, 11, 4), date(2026, 11, 5)


def _session(day: date, level: float) -> pd.DataFrame:
    idx = pd.date_range(f"{day} 09:15", f"{day} 15:30", freq="1min", name="ts")
    return pd.DataFrame({"open": level, "high": level, "low": level, "close": level, "volume": 0.0}, index=idx)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "CACHE_DIR", tmp_path)
    s = DataStore(tmp_path)
    s.save_candles("1minute", "BSE", "SENSEX", _session(DAY1, 77000.0))  # INDmoney has day 1 only
    for name, ex in (("NIFTY", "NSE"), ("INDIAVIX", "NSE")):  # other indices traded both days
        s.save_candles("1minute", ex, name, pd.concat([_session(DAY1, 1.0), _session(DAY2, 1.0)]))
    return s


def test_gap_is_filled_from_yahoo_when_it_matches(store, monkeypatch):
    monkeypatch.setattr(dl, "_yahoo_day", lambda sym, d: _session(d, 77002.0 if d == DAY1 else 77500.0))
    dl.fill_index_gaps(store, "SENSEX", DAY1, DAY2, "1minute")
    day2 = store.load_candles("1minute", "BSE", "SENSEX").loc[str(DAY2):str(DAY2)]
    assert len(day2) == 376 and day2["close"].iloc[0] == 77500.0
    assert '"date": "2026-11-05"' in (store.root / "external_patches.json").read_text()


@pytest.mark.parametrize("yahoo_day1", [
    lambda d: _session(d, 78000.0),           # Yahoo disagrees with INDmoney by 1000 points
    lambda d: pd.DataFrame(),                   # nothing to compare against
])
def test_gap_is_not_filled_when_yahoo_cannot_be_validated(store, monkeypatch, yahoo_day1):
    monkeypatch.setattr(dl, "_yahoo_day", lambda sym, d: yahoo_day1(d) if d == DAY1 else _session(d, 77500.0))
    with pytest.raises(SystemExit):
        dl.fill_index_gaps(store, "SENSEX", DAY1, DAY2, "1minute")
    assert store.load_candles("1minute", "BSE", "SENSEX").loc[str(DAY2):str(DAY2)].empty
