"""Broker-agnostic domain objects shared by the backtester, paper and live bots."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Exchange(str, Enum):
    NSE = "NSE"  # NSE cash / indices
    NFO = "NFO"  # NSE F&O (NIFTY options)
    BSE = "BSE"  # BSE cash / indices
    BFO = "BFO"  # BSE F&O (SENSEX options)


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"  # stop-loss market (SL-M): becomes market once trigger_price trades
    STOP_LIMIT = "STOP_LIMIT"  # stop-loss limit (INDmoney GTT): becomes a limit order at limit_price once triggered


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Instrument:
    symbol: str  # exchange trading symbol, e.g. NIFTY26AUG2523950CE
    exchange: Exchange
    underlying: str  # NIFTY / SENSEX
    lot_size: int = 1
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None

    @property
    def is_option(self) -> bool:
        return self.option_type is not None


@dataclass(frozen=True)
class Bar:
    ts: datetime  # bar open time (IST)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


_order_ids = itertools.count(1)


@dataclass
class Order:
    instrument: Instrument
    side: Side
    quantity: int  # units, not lots (lots * lot_size)
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    trigger_price: float | None = None  # for STOP / STOP_LIMIT orders
    tag: str = ""  # free text for the strategy, e.g. "fly1-short-ce"
    id: int = field(default_factory=lambda: next(_order_ids))
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime | None = None
    triggered_at: datetime | None = None  # STOP_LIMIT: when the trigger traded (the limit may still be unfilled)


@dataclass(frozen=True)
class Fill:
    order_id: int
    instrument: Instrument
    side: Side
    quantity: int
    price: float
    ts: datetime
    charges: float = 0.0
    tag: str = ""


@dataclass
class Position:
    instrument: Instrument
    quantity: int = 0  # signed: + long, - short
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def apply(self, side: Side, qty: int, price: float) -> None:
        signed = qty if side is Side.BUY else -qty
        if self.quantity == 0 or (self.quantity > 0) == (signed > 0):
            total = self.quantity + signed
            self.avg_price = (self.avg_price * abs(self.quantity) + price * qty) / abs(total)
            self.quantity = total
            return

        direction = 1 if self.quantity > 0 else -1
        closing = min(qty, abs(self.quantity))
        self.realized_pnl += closing * (price - self.avg_price) * direction
        self.quantity += signed
        if self.quantity == 0:
            self.avg_price = 0.0
        elif (self.quantity > 0) != (direction > 0):  # flipped through zero
            self.avg_price = price

    def unrealized_pnl(self, mark: float) -> float:
        return self.quantity * (mark - self.avg_price)
