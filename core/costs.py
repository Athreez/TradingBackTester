"""Transaction cost model for Indian index options.

Defaults are INDmoney's (INDstocks API) charges for F&O option orders, as its
own /margin endpoint itemises them for API orders (checked 2026-11-06): Rs 10
brokerage per order; exchange 0.03503% and IPFT 0.0005% of premium on both NSE
and BSE; SEBI 0.0001%; STT 0.15% on sells; stamp duty 0.003% on buys; 18% GST
on brokerage + exchange + SEBI (not IPFT). CostModel.zerodha() keeps the
Zerodha schedule for comparison. Rates change with budgets / exchange
circulars -- re-check periodically. All rates are fractions of premium
turnover unless noted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.models import Exchange, Side


@dataclass(frozen=True)
class CostModel:
    brokerage_per_order: float = 10.0  # flat Rs per executed order (Zerodha: 20)
    stt_sell: float = 0.0015  # STT on sell-side premium
    stt_exercise: float = 0.0015  # STT on intrinsic value of long options exercised at expiry
    exchange_txn: dict[Exchange, float] = field(
        default_factory=lambda: {Exchange.NFO: 0.0003503, Exchange.BFO: 0.0003503})
    ipft: dict[Exchange, float] = field(
        default_factory=lambda: {Exchange.NFO: 0.000005, Exchange.BFO: 0.000005})  # Rs 50 per crore
    sebi_fee: float = 10 / 1e7  # Rs 10 per crore
    stamp_duty_buy: float = 0.00003  # 0.003% on buy side
    gst: float = 0.18  # on brokerage + exchange txn + SEBI fee (+ IPFT if gst_on_ipft)
    gst_on_ipft: bool = False
    brokerage_on_exercise: bool = True  # assume brokerage on contracts settled in the money at expiry
    slippage: float = 0.0  # adverse fraction of price applied to every market fill

    @classmethod
    def zerodha(cls, **overrides) -> "CostModel":
        """Zerodha's F&O options schedule (zerodha.com/charges, checked 2026-11-02)."""
        return cls(**{"brokerage_per_order": 20.0,
                      "exchange_txn": {Exchange.NFO: 0.0003553, Exchange.BFO: 0.000325},
                      "ipft": {Exchange.NFO: 0.01 / 1e7}, "gst_on_ipft": True, **overrides})

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
        gst = (self.brokerage_per_order + txn + sebi + (ipft if self.gst_on_ipft else 0.0)) * self.gst
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
        """Config `costs:` section. `preset: zerodha` starts from the Zerodha schedule."""
        d = dict(d or {})
        preset = d.pop("preset", "indmoney")
        for key in ("exchange_txn", "ipft"):
            if key in d:
                d[key] = {Exchange(k): v for k, v in d[key].items()}
        if preset == "zerodha":
            return cls.zerodha(**d)
        if preset != "indmoney":
            raise ValueError(f"unknown costs preset '{preset}' (indmoney | zerodha)")
        return cls(**d)
