"""What a strategy is allowed to ask about the market.

Backtest, paper and live each provide their own implementation; strategies
only ever see this interface, so the same code runs in all three.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime

import pandas as pd

from core.models import Bar, Instrument


class MarketData(ABC):
    @abstractmethod
    def now(self) -> datetime: ...

    def bar_end(self) -> datetime:
        """Wall-clock time at which the current bar completed. Orders placed now fill at this time."""
        return self.now()

    @abstractmethod
    def spot(self, underlying: str) -> float | None:
        """Latest underlying index price (close of the latest completed bar)."""

    @abstractmethod
    def expiries(self, underlying: str) -> list[date]:
        """Option expiries that are live as of now(), nearest first."""

    @abstractmethod
    def option_chain(self, underlying: str, expiry: date) -> list[Instrument]: ...

    @abstractmethod
    def last_bar(self, instrument: Instrument) -> Bar | None:
        """Latest completed bar at or before now(). Never looks ahead."""

    @abstractmethod
    def history(self, instrument: Instrument, bars: int) -> pd.DataFrame:
        """Up to `bars` completed bars ending at now(), indexed by ts."""

    def ltp(self, instrument: Instrument) -> float | None:
        bar = self.last_bar(instrument)
        return bar.close if bar else None

    def find_option(self, underlying: str, expiry: date, strike: float, option_type) -> Instrument | None:
        for inst in self.option_chain(underlying, expiry):
            if inst.strike == strike and inst.option_type == option_type:
                return inst
        return None
