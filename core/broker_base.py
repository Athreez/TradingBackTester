"""Order execution interface: SimBroker (backtest + paper) and the INDmoney live broker implement this."""
from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import Fill, Instrument, Order, Position


class Broker(ABC):
    @abstractmethod
    def place_order(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: int) -> None: ...

    @abstractmethod
    def open_orders(self) -> list[Order]: ...

    @abstractmethod
    def positions(self) -> dict[Instrument, Position]: ...

    @abstractmethod
    def fills(self) -> list[Fill]: ...

    def position(self, instrument: Instrument) -> Position | None:
        return self.positions().get(instrument)
