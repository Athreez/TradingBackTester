"""Transaction cost model for Indian index options.

Defaults are Zerodha's F&O options charges (zerodha.com/charges, checked
2026-11-02). Rates change with budgets / exchange circulars -- re-check
periodically. All rates are fractions of premium turnover unless noted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.models import Exchange, Side


@dataclass(frozen=True)
class CostModel:
    brokerage_per_order: float = 20.0  # flat Rs per executed order
    stt_sell: float = 0.0015  # STT on sell-side premium
    stt_exercise: float = 0.0015  # STT on intrinsic value of long options exercised at expiry
    exchange_txn: dict[Exchange, float] = field(
        default_factory=lambda: {Exchange.NFO: 0.0003553, Exchange.BFO: 0.000325})
    ipft: dict[Exchange, float] = field(
        default_factory=lambda: {Exchange.NFO: 0.01 / 1e7})  # NSE IPFT: Rs 0.01 per crore
    sebi_fee: float = 10 / 1e7  # Rs 10 per crore
    stamp_duty_buy: float = 0.00003  # 0.003% on buy side
    gst: float = 0.18  # on brokerage + exchange txn + SEBI fee + IPFT
    brokerage_on_exercise: bool = True  # Zerodha charges brokerage on exercised contracts
    slippage: float = 0.0  # adverse fraction of price applied to every market fill

    def txn_rate(self, exchange: Exchange) -> float:
        return self.exchange_txn.get(exchange, 0.0)

    def fill_price(self, side: Side, price: float) -> float:
        return price * (1 + self.slippage) if side is Side.BUY else price * (1 - self.slippage)

    def charges(self, exchange: Exchange, side: Side, quantity: int, price: float) -> float:
        """Total charges for one executed order."""
        turnover = quantity * price
        txn = turnover * self.txn_rate(exchange)
        ipft = turnover * self.ipft.get(exchange, 0.0)
        sebi = turnover * self.sebi_fee
        stt = turnover * self.stt_sell if side is Side.SELL else 0.0
        stamp = turnover * self.stamp_duty_buy if side is Side.BUY else 0.0
        gst = (self.brokerage_per_order + txn + sebi + ipft) * self.gst
        return self.brokerage_per_order + txn + ipft + sebi + stt + stamp + gst

    def settlement_charges(self, was_long: bool, quantity: int, intrinsic: float) -> float:
        """Charges when an option is cash-settled at expiry. Worthless expiry costs nothing."""
        if intrinsic <= 0:
            return 0.0
        brokerage = self.brokerage_per_order if self.brokerage_on_exercise else 0.0
        stt = quantity * intrinsic * self.stt_exercise if was_long else 0.0
        return brokerage * (1 + self.gst) + stt

    @classmethod
    def from_dict(cls, d: dict | None) -> "CostModel":
        if not d:
            return cls()
        d = dict(d)
        for key in ("exchange_txn", "ipft"):
            if key in d:
                d[key] = {Exchange(k): v for k, v in d[key].items()}
        return cls(**d)
