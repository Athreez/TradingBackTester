"""MarketData backed by the local Parquet cache, with a movable clock (no look-ahead)."""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timedelta

import pandas as pd

from core.market_data import MarketData
from core.models import Bar, Instrument, OptionType
from data.store import DataStore
from data.underlyings import get_underlying, index_spec


INDEX_STALE_AFTER = timedelta(minutes=15)
CARRY_RATE = 0.07  # annual; removes the forward carry from put-call parity (fitted: NIFTY ~6.4%, SENSEX ~8.8%)
log = logging.getLogger(__name__)


def _row_to_bar(ts, row) -> Bar:
    return Bar(ts.to_pydatetime(), row.open, row.high, row.low, row.close, row.volume)


class HistoricalMarketData(MarketData):
    def __init__(self, store: DataStore, interval: str):
        self.store = store
        self.interval = interval
        self._now: datetime | None = None
        self._frames: dict[tuple[str, str], pd.DataFrame] = {}
        self._chains: dict[tuple[str, date], list[Instrument]] = {}
        self._synthetic_logged: set[tuple[str, date]] = set()

    # ---- clock ----------------------------------------------------------

    def set_time(self, ts: datetime) -> None:
        self._now = ts

    def now(self) -> datetime:
        if self._now is None:
            raise RuntimeError("clock not started")
        return self._now

    def bar_end(self) -> datetime:
        return self.now() + self._bar_length

    @property
    def _bar_length(self) -> timedelta:
        if self.interval.endswith("minute"):
            return timedelta(minutes=int(self.interval.removesuffix("minute")))
        return timedelta(days=1)

    # ---- internals ------------------------------------------------------

    def _frame(self, exchange: str, symbol: str) -> pd.DataFrame:
        key = (exchange, symbol)
        if key not in self._frames:
            self._frames[key] = self.store.load_candles(self.interval, exchange, symbol)
        return self._frames[key]

    def index_frame(self, name: str) -> pd.DataFrame:
        _, exchange = index_spec(name)
        return self._frame(exchange.value, name.upper())

    # ---- MarketData -----------------------------------------------------

    def index_value(self, name: str) -> float | None:
        """Latest close, or None if the index has no candle from today's session within INDEX_STALE_AFTER
        (a data gap must not silently fall back to yesterday's price)."""
        df = self.index_frame(name)
        i = df.index.searchsorted(self.now(), side="right") - 1
        if i < 0:
            return None
        ts = df.index[i]
        if ts.date() != self.now().date() or self.now() - ts > INDEX_STALE_AFTER:
            return None
        return float(df["close"].iloc[i])

    def spot(self, underlying: str) -> float | None:
        """Index close; if the index feed has a gap (INDmoney had no NIFTY 50 candles on 2026-11-03),
        fall back to the spot implied by the nearest expiry's options."""
        value = self.index_value(underlying)
        if value is None:
            value = self.synthetic_spot(underlying)
        return value

    def synthetic_spot(self, underlying: str) -> float | None:
        """Put-call parity at the strike where CE - PE changes sign: spot = K * exp(-r T) + CE - PE.

        The last known index close (any earlier day) only picks the starting strike; the walk then moves
        towards ATM, so a gap open is fine. Needs fresh candles (today, within INDEX_STALE_AFTER) for both legs."""
        df = self.index_frame(underlying)
        i = df.index.searchsorted(self.now(), side="right") - 1
        expiries = self.expiries(underlying)
        if i < 0 or not expiries:
            return None
        anchor = float(df["close"].iloc[i])
        expiry = expiries[0]
        chain = self.option_chain(underlying, expiry)
        years = max((datetime.combine(expiry, time(15, 30)) - self.now()).total_seconds(), 0) / (365 * 86400)
        discount = math.exp(-CARRY_RATE * years)
        strikes = sorted({inst.strike for inst in chain})
        if not strikes:
            return None
        by_key = {(inst.strike, inst.option_type): inst for inst in chain}

        def diff(k: float) -> float | None:
            legs = [by_key.get((k, t)) for t in (OptionType.CE, OptionType.PE)]
            bars = [self.last_bar(leg) if leg else None for leg in legs]
            if any(b is None or b.ts.date() != self.now().date() or self.now() - b.ts > INDEX_STALE_AFTER
                   for b in bars):
                return None
            return bars[0].close - bars[1].close

        pos = min(range(len(strikes)), key=lambda j: abs(strikes[j] - anchor))
        best: tuple[float, float] | None = None  # (|CE - PE|, implied spot)
        for step in (1, -1):  # CE - PE falls as the strike rises: walk until it changes sign
            j, prev = pos, None
            while 0 <= j < len(strikes) and abs(strikes[j] - anchor) <= 0.1 * anchor:
                d = diff(strikes[j])
                if d is not None:
                    if best is None or abs(d) < best[0]:
                        best = (abs(d), strikes[j] * discount + d)
                    if prev is not None and (d > 0) != (prev > 0):
                        break
                    if (d > 0 and step < 0) or (d < 0 and step > 0):
                        break  # ATM lies the other way
                    prev = d
                j += step
        if best is not None and (underlying, self.now().date()) not in self._synthetic_logged:
            self._synthetic_logged.add((underlying, self.now().date()))
            log.warning("%s %s: no index candles, using spot implied by options (K + CE - PE) = %.2f",
                        self.now().date(), underlying, best[1])
        return best[1] if best else None

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
                    lot_size=u.lot_size(expiry), expiry=expiry, strike=float(r.strike_price),
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
