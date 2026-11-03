"""Live 1-minute candles from INDmoney, served through the same MarketData the backtester uses.

MemoryStore replaces the Parquet cache: the day's contracts come from the live
instrument master, and candles are appended as each minute completes. Wrapping
it in HistoricalMarketData means the strategy sees exactly what it sees in a
backtest (no look-ahead, fills on the next bar's open).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable

import pandas as pd

from data.indmoney_client import IndMoneyClient
from data.store import DataStore
from data.underlyings import get_underlying, index_spec

log = logging.getLogger(__name__)

EMPTY = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], name="ts"))
IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = time(9, 15)


def option_symbol(underlying: str, expiry: date, strike: float, option_type: str) -> str:
    """Unique per contract, in the format INDmoney uses for expired weeklies (NIFTY26NOV0323600PE).
    The live master's TRADING_SYMBOL (NIFTY-Nov2026-23600-PE) is shared by every expiry in the month."""
    return f"{underlying}{expiry:%y%b%d}".upper() + f"{strike:g}{option_type}"


class MemoryStore(DataStore):
    """In-memory DataStore for one trading day (index scrip codes still come from data/cache).

    fetch_missing(exchange, symbol) is called the first time a series is asked for, so a strategy that
    picks strikes mid-replay gets their candles from the session open (fills stay on the right bar)."""

    def __init__(self, fetch_missing: Callable[[str, str], pd.DataFrame | None] | None = None):
        super().__init__()
        self._candles: dict[tuple[str, str], pd.DataFrame] = {}
        self._contracts: dict[tuple[str, date], pd.DataFrame] = {}
        self.fetch_missing = fetch_missing
        self._fetched: set[tuple[str, str]] = set()

    def save_candles(self, interval, exchange, symbol, df):
        if df.empty:
            return
        old = self._candles.get((exchange, symbol))
        if old is not None:
            df = pd.concat([old, df])
            df = df[~df.index.duplicated(keep="last")]
        self._candles[(exchange, symbol)] = df.sort_index()

    def load_candles(self, interval, exchange, symbol):
        key = (exchange, symbol)
        if key not in self._candles and key not in self._fetched and self.fetch_missing:
            df = self.fetch_missing(exchange, symbol)
            if df is not None:  # None = fetch failed; try again on the next request
                self._fetched.add(key)
                self.save_candles(interval, exchange, symbol, df)
        return self._candles.get(key, EMPTY)

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
        self._day: date | None = None
        store.fetch_missing = self._fetch_session

    def _fetch_session(self, exchange: str, symbol: str) -> pd.DataFrame | None:
        """Today's candles so far for one option (used by MemoryStore on first access)."""
        code = self._symbol_to_code.get(symbol)
        if code is None or self._day is None:
            return EMPTY  # not an option we know (e.g. an index): nothing to fetch
        start = datetime.combine(self._day, SESSION_OPEN)
        end = datetime.now(IST).replace(tzinfo=None)
        try:
            return self.client.historical([code], self.interval, start, end).get(code, EMPTY)
        except Exception as e:  # network hiccup: the next poll fetches it instead
            log.warning("fetching %s failed: %s", symbol, e)
            return None

    def load_contracts(self, today: date, expiries_per_underlying: int = 2) -> None:
        """Today's tradable option contracts (nearest expiries) from the live instrument master."""
        self._day = today
        master = self.client.instruments("fno")
        master = master[master["OPTION_TYPE"].isin(["CE", "PE"])]
        for name in self.indices:
            index_name, exchange = index_spec(name)
            code = self.store.get_index_code(name)
            if code is None:  # fresh install: look it up once and cache it, like the downloader does
                code = self.client.index_scrip_code(index_name, exchange.value)
                self.store.set_index_code(name, code)
                log.info("Resolved %s index scrip code: %s", name, code)
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
                strikes = chain["STRIKE_PRICE"].astype(float).values
                symbols = [option_symbol(name, expiry, k, t) for k, t in zip(strikes, chain["OPTION_TYPE"])]
                self.store.save_contracts(name, expiry, pd.DataFrame({
                    "trading_symbol": symbols,
                    "strike_price": strikes,
                    "option_type": chain["OPTION_TYPE"].values,
                    "expiry": expiry,
                }))
                for sym, sec_id in zip(symbols, chain["SECURITY_ID"]):
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
