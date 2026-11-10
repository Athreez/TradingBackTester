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
    # 50 from the Jul-2021 series (earlier weeklies were 75 and are not modelled); 25 for contracts
    # trading from 26-Apr-2024; 75 for contracts listed from 20-Nov-2024, i.e. weekly 25 through the
    # 26-Dec-2024 expiry and 75 from 02-Jan-2025; NSE circular FAOP70616: 65 from 06-Jan-2026.
    "NIFTY": Underlying("NIFTY", "NIFTY 50", Exchange.NSE, Exchange.NFO, 50,
                        ((date(2021, 8, 1), 50), (date(2024, 4, 26), 25), (date(2024, 12, 27), 75),
                         (date(2025, 12, 24), 65))),
    # 10 since the May-2023 weekly relaunch; 20 for contracts listed from 20-Nov-2024 (weekly 10 through
    # the 03-Jan-2025 expiry, 20 from 07-Jan-2025); 25 from the Jan-2027 contract.
    "SENSEX": Underlying("SENSEX", "SENSEX", Exchange.BSE, Exchange.BFO, 100,
                         ((date(2023, 5, 1), 10), (date(2025, 1, 4), 20), (date(2027, 1, 1), 25))),
}


def get_underlying(name: str) -> Underlying:
    try:
        return UNDERLYINGS[name.upper()]
    except KeyError:
        raise ValueError(f"Unknown underlying '{name}'. Known: {list(UNDERLYINGS)}") from None


# Indices we download for signals but do not trade options on.
SIGNAL_INDICES: dict[str, tuple[str, Exchange]] = {
    "INDIAVIX": ("India VIX", Exchange.NSE),
}


def index_spec(name: str) -> tuple[str, Exchange]:
    """(name in the INDstocks index master, exchange) for a tradable underlying or a signal index."""
    key = name.upper()
    if key in UNDERLYINGS:
        u = UNDERLYINGS[key]
        return u.index_name, u.index_exchange
    if key in SIGNAL_INDICES:
        return SIGNAL_INDICES[key]
    raise ValueError(f"Unknown index '{name}'. Known: {list(UNDERLYINGS) + list(SIGNAL_INDICES)}")


def option_symbol(underlying: str, expiry: date, strike: float, option_type: str) -> str:
    """Unique per contract, in the format INDmoney uses for expired weeklies (NIFTY26NOV0323600PE).
    The live master's TRADING_SYMBOL (NIFTY-Nov2026-23600-PE) is shared by every expiry in the month."""
    return f"{underlying}{expiry:%y%b%d}".upper() + f"{strike:g}{option_type}"
