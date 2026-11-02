"""Static facts about the underlyings we trade."""
from __future__ import annotations

from dataclasses import dataclass

from core.models import Exchange


@dataclass(frozen=True)
class Underlying:
    name: str  # as used in option trading symbols / contract APIs
    index_name: str  # as listed in the INDstocks `source=index` instrument master
    index_exchange: Exchange
    option_exchange: Exchange
    strike_step: int


UNDERLYINGS: dict[str, Underlying] = {
    "NIFTY": Underlying("NIFTY", "NIFTY 50", Exchange.NSE, Exchange.NFO, 50),
    "SENSEX": Underlying("SENSEX", "SENSEX", Exchange.BSE, Exchange.BFO, 100),
}


def get_underlying(name: str) -> Underlying:
    try:
        return UNDERLYINGS[name.upper()]
    except KeyError:
        raise ValueError(f"Unknown underlying '{name}'. Known: {list(UNDERLYINGS)}") from None
