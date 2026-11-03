from datetime import date

import pandas as pd

from paper_trader.live_feed import LiveFeed, MemoryStore, option_symbol


class FakeClient:
    """Live master where two expiries share one TRADING_SYMBOL, as INDmoney's does."""

    def __init__(self):
        self.requested: list[list[str]] = []

    def instruments(self, source):
        return pd.DataFrame({
            "TRADING_SYMBOL": ["NIFTY-Nov2026-23600-PE"] * 2,
            "OPTION_TYPE": ["PE", "PE"],
            "EXPIRY_DATE": ["11/03/2026 14:00", "11/09/2026 14:00"],
            "LOT_UNITS": [65, 65],
            "STRIKE_PRICE": [23600.0, 23600.0],
            "SECURITY_ID": [54065, 39944],
        })

    def index_scrip_code(self, index_name, exchange):
        return {"NIFTY 50": "NSE_40000001", "INDIA VIX": "NSE_40000107"}[index_name.upper()]

    def historical(self, codes, interval, start, end):
        self.requested.append(codes)
        idx = pd.DatetimeIndex([pd.Timestamp("2026-11-03 09:15")], name="ts")
        return {c: pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [0]}, index=idx)
                for c in codes}


def make_feed(tmp_path, cached_codes: bool = True):
    store = MemoryStore()
    store.root = tmp_path
    if cached_codes:
        for name, code in (("NIFTY", "NSE_40000001"), ("INDIAVIX", "NSE_40000107")):
            store.set_index_code(name, code)
    client = FakeClient()
    feed = LiveFeed(client, store, ["NIFTY"])
    feed.load_contracts(date(2026, 11, 3))
    return feed, store, client


def test_option_symbol_matches_expired_format():
    assert option_symbol("NIFTY", date(2026, 10, 6), 24000.0, "CE") == "NIFTY26OCT0624000CE"


def test_expiries_sharing_a_trading_symbol_get_their_own_contract(tmp_path):
    feed, store, _ = make_feed(tmp_path)
    near = store.load_contracts("NIFTY", date(2026, 11, 3))["trading_symbol"].tolist()
    far = store.load_contracts("NIFTY", date(2026, 11, 9))["trading_symbol"].tolist()
    assert near == ["NIFTY26NOV0323600PE"] and far == ["NIFTY26NOV0923600PE"]
    assert feed._symbol_to_code["NIFTY26NOV0323600PE"] == "NFO_54065"
    assert feed._symbol_to_code["NIFTY26NOV0923600PE"] == "NFO_39944"


def test_option_candles_are_fetched_once_on_first_access(tmp_path):
    _, store, client = make_feed(tmp_path)
    assert len(store.load_candles("1minute", "NFO", "NIFTY26NOV0323600PE")) == 1
    store.load_candles("1minute", "NFO", "NIFTY26NOV0323600PE")
    assert client.requested == [["NFO_54065"]]


def test_missing_index_codes_are_resolved_and_cached(tmp_path):
    feed, store, _ = make_feed(tmp_path, cached_codes=False)
    assert store.get_index_code("NIFTY") == "NSE_40000001"
    assert store.get_index_code("INDIAVIX") == "NSE_40000107"
    assert feed._code_to_key["NSE_40000001"] == ("NSE", "NIFTY")
