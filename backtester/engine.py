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
from core.market_data import MarketData
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
            run_bar(self.strategy, ctx, self.broker, self.data, ts)

            is_day_end = i + 1 == len(self.timeline) or self.timeline[i + 1].date() != ts.date()
            if is_day_end:
                for fill in settle_expiring(self.broker, self.data, ts):
                    self.strategy.on_fill(ctx, fill)
            equity.append((ts, self.broker.equity(self.data.ltp)))

        self.strategy.on_stop(ctx)
        series = pd.Series(dict(equity), name="equity")
        series.index.name = "ts"
        return BacktestResult(series, self.broker.fills(), self.broker)


def run_bar(strategy: Strategy, ctx: Context, broker: SimBroker, data: HistoricalMarketData,
            ts: datetime) -> list[Fill]:
    """One bar: fill pending orders at this bar's open, then let the strategy see the completed bar.
    Shared by the backtester and the paper trader so both follow identical rules."""
    data.set_time(ts)
    fills = broker.process(ts)
    for fill in fills:
        strategy.on_fill(ctx, fill)
    strategy.on_bar(ctx)
    return fills


def settle_expiring(broker: SimBroker, data: MarketData, ts: datetime) -> list[Fill]:
    """Cash-settle options expiring on ts's date at intrinsic value against the index."""
    fills = []
    for inst in list(broker.positions()):
        if not inst.is_option or inst.expiry is None or inst.expiry > ts.date():
            continue
        spot = data.spot(inst.underlying)
        if spot is None:
            continue
        if inst.option_type is OptionType.CE:
            intrinsic = max(0.0, spot - inst.strike)
        else:
            intrinsic = max(0.0, inst.strike - spot)
        fill = broker.settle(inst, intrinsic, ts)
        if fill:
            fills.append(fill)
    return fills
