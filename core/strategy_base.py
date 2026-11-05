"""Base class every strategy in strategies/<name>/strategy.py subclasses."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.broker_base import Broker
from core.market_data import MarketData
from core.models import Fill, Instrument, Order, OrderType, Side


@dataclass
class Context:
    data: MarketData
    broker: Broker

    @property
    def now(self) -> datetime:
        return self.data.now()

    def buy(self, instrument: Instrument, lots: int, limit: float | None = None, tag: str = "") -> Order:
        return self._order(instrument, Side.BUY, lots, limit, tag)

    def sell(self, instrument: Instrument, lots: int, limit: float | None = None, tag: str = "") -> Order:
        return self._order(instrument, Side.SELL, lots, limit, tag)

    def stop_buy(self, instrument: Instrument, lots: int, trigger: float, tag: str = "",
                 limit: float | None = None) -> Order:
        """Stop-loss buy (e.g. SL for a short option): fires once price trades >= trigger, then fills at
        market, or with `limit` as a limit order at that price (stop-limit, like an INDmoney GTT)."""
        return self._order(instrument, Side.BUY, lots, limit, tag, trigger)

    def stop_sell(self, instrument: Instrument, lots: int, trigger: float, tag: str = "") -> Order:
        return self._order(instrument, Side.SELL, lots, None, tag, trigger)

    def cancel_all(self) -> None:
        for order in self.broker.open_orders():
            self.broker.cancel_order(order.id)

    def square_off_all(self, tag: str = "square-off") -> None:
        for inst, pos in self.broker.positions().items():
            if pos.quantity > 0:
                self.sell(inst, pos.quantity // inst.lot_size, tag=tag)
            elif pos.quantity < 0:
                self.buy(inst, -pos.quantity // inst.lot_size, tag=tag)

    def _order(self, instrument, side, lots, limit, tag, trigger=None) -> Order:
        if trigger is not None:
            order_type = OrderType.STOP if limit is None else OrderType.STOP_LIMIT
        elif limit is not None:
            order_type = OrderType.LIMIT
        else:
            order_type = OrderType.MARKET
        order = Order(
            instrument=instrument,
            side=side,
            quantity=lots * instrument.lot_size,
            order_type=order_type,
            limit_price=limit,
            trigger_price=trigger,
            tag=tag,
            created_at=self.now,
        )
        return self.broker.place_order(order)


class Strategy(ABC):
    """Lifecycle: on_start -> on_bar (every bar) / on_fill (every fill) -> on_stop.

    `params` comes from strategies/<name>/config.yaml (`params:` section).
    Orders placed in on_bar fill at the NEXT bar's open in backtests (no look-ahead).
    """

    def __init__(self, params: dict[str, Any]):
        self.params = params

    def on_start(self, ctx: Context) -> None:
        pass

    @abstractmethod
    def on_bar(self, ctx: Context) -> None: ...

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        pass

    def on_stop(self, ctx: Context) -> None:
        pass
