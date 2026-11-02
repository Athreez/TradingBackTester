"""Event loop: replays historical bars through a strategy.

Per bar `ts` (bar open time):
  1. pending orders fill at this bar's open (orders from the previous bar)
  2. strategy.on_bar sees this bar as completed
  3. equity is marked to the latest close
At the end of each trading day, options expiring that day are settled at
intrinsic value against the index close.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from backtester.historical_data import HistoricalMarketData
from backtester.sim_broker import SimBroker
from core.models import Fill, OptionType
from core.strategy_base import Context, Strategy


@dataclass
class BacktestResult:
    equity: pd.Series
    fills: list[Fill]
    broker: SimBroker
    meta: dict = field(default_factory=dict)


class BacktestEngine:
    def __init__(self, strategy: Strategy, data: HistoricalMarketData, broker: SimBroker,
                 timeline: list[datetime]):
        self.strategy = strategy
        self.data = data
        self.broker = broker
        self.timeline = timeline

    def run(self) -> BacktestResult:
        if not self.timeline:
            raise ValueError("Empty timeline -- is index data downloaded for this date range?")
        ctx = Context(self.data, self.broker)
        equity: list[tuple[datetime, float]] = []

        self.data.set_time(self.timeline[0])
        self.strategy.on_start(ctx)

        for i, ts in enumerate(self.timeline):
            self.data.set_time(ts)
            for fill in self.broker.process(ts):
                self.strategy.on_fill(ctx, fill)
            self.strategy.on_bar(ctx)

            is_day_end = i + 1 == len(self.timeline) or self.timeline[i + 1].date() != ts.date()
            if is_day_end:
                for fill in self._settle_expiring(ts):
                    self.strategy.on_fill(ctx, fill)
            equity.append((ts, self.broker.equity(self.data.ltp)))

        self.strategy.on_stop(ctx)
        series = pd.Series(dict(equity), name="equity")
        series.index.name = "ts"
        return BacktestResult(series, self.broker.fills(), self.broker)

    def _settle_expiring(self, ts: datetime) -> list[Fill]:
        fills = []
        for inst in list(self.broker.positions()):
            if not inst.is_option or inst.expiry is None or inst.expiry > ts.date():
                continue
            spot = self.data.spot(inst.underlying)
            if spot is None:
                continue
            intrinsic = max(0.0, spot - inst.strike) if inst.option_type is OptionType.CE \
                else max(0.0, inst.strike - spot)
            fill = self.broker.settle(inst, intrinsic, ts)
            if fill:
                fills.append(fill)
        return fills
