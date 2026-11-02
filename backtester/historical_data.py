"""MarketData backed by the local Parquet cache, with a movable clock (no look-ahead)."""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from core.market_data import MarketData
from core.models import Bar, Instrument, OptionType
from data.store import DataStore
from data.underlyings import get_underlying


def _row_to_bar(ts, row) -> Bar:
    return Bar(ts.to_pydatetime(), row.open, row.high, row.low, row.close, row.volume)


class HistoricalMarketData(MarketData):
    def __init__(self, store: DataStore, interval: str):
        self.store = store
        self.interval = interval
        self._now: datetime | None = None
        self._frames: dict[tuple[str, str], pd.DataFrame] = {}
        self._chains: dict[tuple[str, date], list[Instrument]] = {}

    # ---- clock ----------------------------------------------------------

    def set_time(self, ts: datetime) -> None:
        self._now = ts

    def now(self) -> datetime:
        if self._now is None:
            raise RuntimeError("clock not started")
        return self._now

    # ---- internals ------------------------------------------------------

    def _frame(self, exchange: str, symbol: str) -> pd.DataFrame:
        key = (exchange, symbol)
        if key not in self._frames:
            self._frames[key] = self.store.load_candles(self.interval, exchange, symbol)
        return self._frames[key]

    def index_frame(self, underlying: str) -> pd.DataFrame:
        u = get_underlying(underlying)
        return self._frame(u.index_exchange.value, u.name)

    # ---- MarketData -----------------------------------------------------

    def spot(self, underlying: str) -> float | None:
        df = self.index_frame(underlying)
        i = df.index.searchsorted(self.now(), side="right") - 1
        return float(df["close"].iloc[i]) if i >= 0 else None

    def expiries(self, underlying: str) -> list[date]:
        today = self.now().date()
        return [e for e in self.store.expiries(underlying) if e >= today]

    def option_chain(self, underlying: str, expiry: date) -> list[Instrument]:
        key = (underlying, expiry)
        if key not in self._chains:
            u = get_underlying(underlying)
            df = self.store.load_contracts(underlying, expiry)
            if not df.empty:
                df = df[df["option_type"].isin(["CE", "PE"])]
            self._chains[key] = [
                Instrument(
                    symbol=r.trading_symbol, exchange=u.option_exchange, underlying=underlying,
                    lot_size=int(r.lot_size), expiry=expiry, strike=float(r.strike_price),
                    option_type=OptionType(r.option_type),
                )
                for r in df.itertuples()
            ]
        return self._chains[key]

    def last_bar(self, instrument: Instrument) -> Bar | None:
        df = self._frame(instrument.exchange.value, instrument.symbol)
        i = df.index.searchsorted(self.now(), side="right") - 1
        return _row_to_bar(df.index[i], df.iloc[i]) if i >= 0 else None

    def bar_at(self, instrument: Instrument, ts: datetime) -> Bar | None:
        """The bar that opens exactly at ts (used by SimBroker to fill orders)."""
        df = self._frame(instrument.exchange.value, instrument.symbol)
        if ts in df.index:
            return _row_to_bar(pd.Timestamp(ts), df.loc[ts])
        return None

    def history(self, instrument: Instrument, bars: int) -> pd.DataFrame:
        df = self._frame(instrument.exchange.value, instrument.symbol)
        end = df.index.searchsorted(self.now(), side="right")
        return df.iloc[max(0, end - bars):end]
