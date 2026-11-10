"""Simulated broker used by both the backtester and the paper trading bot.

Orders are queued and filled on the next `process()` call against the bar the
`quote` callback returns (next bar's open in backtests, live LTP in paper).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from core.broker_base import Broker
from core.costs import CostModel
from core.models import Bar, Fill, Instrument, Order, OrderStatus, OrderType, Position, Side

QuoteFn = Callable[[Instrument], Bar | None]


class SimBroker(Broker):
    def __init__(self, quote: QuoteFn, costs: CostModel, initial_capital: float):
        self._quote = quote
        self.costs = costs
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.total_charges = 0.0
        self._pending: dict[int, Order] = {}
        self._orders: list[Order] = []
        self._fills: list[Fill] = []
        self._positions: dict[Instrument, Position] = {}

    # ---- Broker ---------------------------------------------------------

    def place_order(self, order: Order) -> Order:
        if order.quantity <= 0:
            order.status = OrderStatus.REJECTED
        else:
            self._pending[order.id] = order
        self._orders.append(order)
        return order

    def cancel_order(self, order_id: int) -> None:
        order = self._pending.pop(order_id, None)
        if order:
            order.status = OrderStatus.CANCELLED

    def open_orders(self) -> list[Order]:
        return list(self._pending.values())

    def positions(self) -> dict[Instrument, Position]:
        return {i: p for i, p in self._positions.items() if p.quantity != 0}

    def fills(self) -> list[Fill]:
        return list(self._fills)

    def orders(self) -> list[Order]:
        return list(self._orders)

    # ---- simulation -----------------------------------------------------

    def process(self, ts: datetime) -> list[Fill]:
        """Try to fill every pending order against the current quote."""
        new_fills = []
        for order in list(self._pending.values()):
            bar = self._quote(order.instrument)
            if bar is None:
                continue  # no trade in this instrument this bar; stay pending
            price = self._match(order, bar, ts)
            if price is None:
                continue
            del self._pending[order.id]
            order.status = OrderStatus.FILLED
            new_fills.append(self._record_fill(order, price, ts))
        return new_fills

    def settle(self, instrument: Instrument, price: float, ts: datetime) -> Fill | None:
        """Cash-settle a position at a fixed price (intrinsic value at expiry)."""
        pos = self._positions.get(instrument)
        if not pos or pos.quantity == 0:
            return None
        was_long = pos.quantity > 0
        side = Side.SELL if was_long else Side.BUY
        order = Order(instrument, side, abs(pos.quantity), tag="expiry-settlement",
                      status=OrderStatus.FILLED, created_at=ts)
        self._orders.append(order)
        charges = self.costs.settlement_charges(was_long, order.quantity, price, ts.date())
        return self._record_fill(order, price, ts, charges=charges)

    def equity(self, mark: Callable[[Instrument], float | None]) -> float:
        value = self.cash
        for inst, pos in self.positions().items():
            price = mark(inst)
            value += pos.quantity * (price if price is not None else pos.avg_price)
        return value

    def _match(self, order: Order, bar: Bar, ts: datetime) -> float | None:
        if order.order_type is OrderType.MARKET:
            return self.costs.fill_price(order.side, bar.open)
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and order.triggered_at is None:
            trigger = order.trigger_price
            hit = bar.high >= trigger if order.side is Side.BUY else bar.low <= trigger
            if not hit:
                return None
            if order.order_type is OrderType.STOP:
                price = max(bar.open, trigger) if order.side is Side.BUY else min(bar.open, trigger)
                return self.costs.fill_price(order.side, price)
            order.triggered_at = ts
            # Becomes a limit order: fills where the trigger was crossed if that is inside the limit;
            # if price gapped past the limit it rests there (filled later only if price comes back).
            start = max(bar.open, trigger) if order.side is Side.BUY else min(bar.open, trigger)
            limit = order.limit_price
            if order.side is Side.BUY:
                return min(self.costs.fill_price(Side.BUY, start), limit) if start <= limit else None
            return max(self.costs.fill_price(Side.SELL, start), limit) if start >= limit else None
        return self._limit_fill(order, bar)

    def _limit_fill(self, order: Order, bar: Bar) -> float | None:
        limit = order.limit_price
        if order.side is Side.BUY and bar.low <= limit:
            return min(bar.open, limit)
        if order.side is Side.SELL and bar.high >= limit:
            return max(bar.open, limit)
        return None

    def _record_fill(self, order: Order, price: float, ts: datetime, charges: float | None = None) -> Fill:
        inst = order.instrument
        if charges is None:
            charges = self.costs.charges(inst.exchange, order.side, order.quantity, price, ts.date())
        fill = Fill(order.id, inst, order.side, order.quantity, price, ts, charges, order.tag)
        signed_value = order.quantity * price
        self.cash += -signed_value if order.side is Side.BUY else signed_value
        self.cash -= charges
        self.total_charges += charges
        self._positions.setdefault(inst, Position(inst)).apply(order.side, order.quantity, price)
        self._fills.append(fill)
        return fill
