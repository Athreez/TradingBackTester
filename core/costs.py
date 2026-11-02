"""Transaction cost model for Indian index options.

Rates change with budgets / exchange circulars -- verify against your broker's
charges page before trusting backtest numbers. All rates are fractions of
premium turnover unless noted.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.models import Exchange, Side


@dataclass(frozen=True)
class CostModel:
    brokerage_per_order: float = 20.0  # flat Rs/order (INDmoney: 10, Zerodha: 20)
    stt_sell: float = 0.001  # STT on sell-side option premium
    exchange_txn: dict[Exchange, float] | None = None
    sebi_fee: float = 10 / 1e7  # Rs 10 per crore
    stamp_duty_buy: float = 0.00003
    gst: float = 0.18  # on brokerage + exchange txn + SEBI fee
    slippage: float = 0.0  # adverse fraction of price applied to every fill

    def txn_rate(self, exchange: Exchange) -> float:
        rates = self.exchange_txn
        if rates is None:
            rates = {Exchange.NFO: 0.0003503, Exchange.BFO: 0.000325}
        return rates.get(exchange, 0.0)

    def fill_price(self, side: Side, price: float) -> float:
        return price * (1 + self.slippage) if side is Side.BUY else price * (1 - self.slippage)

    def charges(self, exchange: Exchange, side: Side, quantity: int, price: float) -> float:
        turnover = quantity * price
        txn = turnover * self.txn_rate(exchange)
        sebi = turnover * self.sebi_fee
        stt = turnover * self.stt_sell if side is Side.SELL else 0.0
        stamp = turnover * self.stamp_duty_buy if side is Side.BUY else 0.0
        gst = (self.brokerage_per_order + txn + sebi) * self.gst
        return self.brokerage_per_order + txn + sebi + stt + stamp + gst

    @classmethod
    def from_dict(cls, d: dict | None) -> "CostModel":
        if not d:
            return cls()
        d = dict(d)
        if "exchange_txn" in d:
            d["exchange_txn"] = {Exchange(k): v for k, v in d["exchange_txn"].items()}
        return cls(**d)
