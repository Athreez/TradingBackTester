"""Live 1-minute candles from INDmoney, served through the same MarketData the backtester uses.

MemoryStore replaces the Parquet cache: the day's contracts come from the live
instrument master, and candles are appended as each minute completes. Wrapping
it in HistoricalMarketData means the strategy sees exactly what it sees in a
backtest (no look-ahead, fills on the next bar's open).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

import pandas as pd

from data.indmoney_client import IndMoneyClient
from data.store import DataStore
from data.underlyings import get_underlying, index_spec

log = logging.getLogger(__name__)

EMPTY = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], name="ts"))


class MemoryStore(DataStore):
    """In-memory DataStore for one trading day (index scrip codes still come from data/cache)."""

    def __init__(self):
        super().__init__()
        self._candles: dict[tuple[str, str], pd.DataFrame] = {}
        self._contracts: dict[tuple[str, date], pd.DataFrame] = {}

    def save_candles(self, interval, exchange, symbol, df):
        if df.empty:
            return
        old = self._candles.get((exchange, symbol))
        if old is not None:
            df = pd.concat([old, df])
            df = df[~df.index.duplicated(keep="last")]
        self._candles[(exchange, symbol)] = df.sort_index()

    def load_candles(self, interval, exchange, symbol):
        return self._candles.get((exchange, symbol), EMPTY)

    def has_candles(self, interval, exchange, symbol):
        return (exchange, symbol) in self._candles

    def save_contracts(self, underlying, expiry, df):
        self._contracts[(underlying, expiry)] = df

    def load_contracts(self, underlying, expiry):
        return self._contracts.get((underlying, expiry), pd.DataFrame())

    def expiries(self, underlying):
        return sorted(e for u, e in self._contracts if u == underlying)


class LiveFeed:
    def __init__(self, client: IndMoneyClient, store: MemoryStore, underlyings: list[str],
                 signal_indices: tuple[str, ...] = ("INDIAVIX",), interval: str = "1minute"):
        self.client = client
        self.store = store
        self.underlyings = underlyings
        self.interval = interval
        self.indices = [*underlyings, *signal_indices]
        self._code_to_key: dict[str, tuple[str, str]] = {}  # scrip code -> (exchange, symbol)
        self._symbol_to_code: dict[str, str] = {}

    def load_contracts(self, today: date, expiries_per_underlying: int = 2) -> None:
        """Today's tradable option contracts (nearest expiries) from the live instrument master."""
        master = self.client.instruments("fno")
        master = master[master["OPTION_TYPE"].isin(["CE", "PE"])]
        for name in self.indices:
            _, exchange = index_spec(name)
            code = self.store.get_index_code(name)
            if code is None:
                raise RuntimeError(f"No scrip code for {name}; run `python -m data.downloader index --underlying {name} ...` once")
            self._code_to_key[code] = (exchange.value, name)

        for name in self.underlyings:
            u = get_underlying(name)
            rows = master[master["TRADING_SYMBOL"].astype(str).str.startswith(f"{name}-")].copy()
            rows["expiry"] = pd.to_datetime(rows["EXPIRY_DATE"], format="%m/%d/%Y %H:%M").dt.date
            expiries = sorted(e for e in rows["expiry"].unique() if e >= today)[:expiries_per_underlying]
            for expiry in expiries:
                chain = rows[rows["expiry"] == expiry]
                lots = set(chain["LOT_UNITS"].astype(int))
                if lots != {u.lot_size(expiry)}:
                    log.warning("%s %s: live lot size %s != our table %s -- update data/underlyings.py",
                                name, expiry, lots, u.lot_size(expiry))
                self.store.save_contracts(name, expiry, pd.DataFrame({
                    "trading_symbol": chain["TRADING_SYMBOL"].values,
                    "strike_price": chain["STRIKE_PRICE"].astype(float).values,
                    "option_type": chain["OPTION_TYPE"].values,
                    "expiry": expiry,
                }))
                for sym, sec_id in zip(chain["TRADING_SYMBOL"], chain["SECURITY_ID"]):
                    code = f"{u.option_exchange.value}_{int(sec_id)}"
                    self._code_to_key[code] = (u.option_exchange.value, sym)
                    self._symbol_to_code[sym] = code
            log.info("%s: loaded %d contracts for expiries %s", name,
                     int((rows["expiry"].isin(expiries)).sum()), [str(e) for e in expiries])

    def poll(self, symbols: list[str], start: datetime, end: datetime) -> None:
        """Fetch candles in [start, end) for all indices plus the given option symbols."""
        codes = [c for c, (_, sym) in self._code_to_key.items() if sym in self.indices]
        codes += [self._symbol_to_code[s] for s in symbols if s in self._symbol_to_code]
        frames = self.client.historical(codes, self.interval, start, end)
        for code, df in frames.items():
            exchange, symbol = self._code_to_key[code]
            self.store.save_candles(self.interval, exchange, symbol, df)

    def index_bars(self, after: datetime | None, upto: datetime) -> list[datetime]:
        """Completed index bar timestamps in (after, upto], across all underlyings."""
        out = set()
        for name in self.underlyings:
            idx = self.store.load_candles(self.interval, *index_spec_key(name)).index
            sel = idx[(idx <= upto) & ((idx > after) if after else True)]
            out.update(sel)
        return [t.to_pydatetime() for t in sorted(out)]


def index_spec_key(name: str) -> tuple[str, str]:
    _, exchange = index_spec(name)
    return exchange.value, name.upper()
