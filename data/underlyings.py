"""Static facts about the underlyings we trade."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.models import Exchange


@dataclass(frozen=True)
class Underlying:
    name: str  # as used in option trading symbols / contract APIs
    index_name: str  # as listed in the INDstocks `source=index` instrument master
    index_exchange: Exchange
    option_exchange: Exchange
    strike_step: int
    # (first expiry with this lot size, lot size), oldest first. INDmoney's expired-contract
    # lot sizes are unreliable (garbage for NIFTY, missing for SENSEX), so we keep our own.
    lot_sizes: tuple[tuple[date, int], ...]

    def lot_size(self, expiry: date) -> int:
        size = None
        for first_expiry, lot in self.lot_sizes:
            if expiry >= first_expiry:
                size = lot
        if size is None:
            raise ValueError(f"No lot size known for {self.name} expiry {expiry}; extend lot_sizes")
        return size


UNDERLYINGS: dict[str, Underlying] = {
    # NSE circular FAOP70616: weekly 75 until the 23-Dec-2025 expiry, 65 from 06-Jan-2026.
    "NIFTY": Underlying("NIFTY", "NIFTY 50", Exchange.NSE, Exchange.NFO, 50,
                        ((date(2024, 11, 20), 75), (date(2025, 12, 24), 65))),
    # 20 for every expiry through Dec 2026 (INDstocks instrument master); 25 from the Jan-2027 contract.
    "SENSEX": Underlying("SENSEX", "SENSEX", Exchange.BSE, Exchange.BFO, 100,
                         ((date(2024, 12, 1), 20), (date(2027, 1, 1), 25))),
}


def get_underlying(name: str) -> Underlying:
    try:
        return UNDERLYINGS[name.upper()]
    except KeyError:
        raise ValueError(f"Unknown underlying '{name}'. Known: {list(UNDERLYINGS)}") from None
