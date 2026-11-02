"""Intraday iron fly on the nearest weekly expiry.

At entry_time: buy hedges at ATM +/- hedge_distance, sell ATM CE + PE.
Each short leg gets a stop-loss at entry * (1 + stop_loss_pct/100); when it
fires, that side's hedge is sold too. Everything left is squared off at exit_time.
Which underlying trades depends on the weekday (see config.yaml `schedule`).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from core.models import Fill, Instrument, OptionType
from core.strategy_base import Context, Strategy

log = logging.getLogger(__name__)

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4}


class TripleIronFly(Strategy):
    def __init__(self, params):
        super().__init__(params)
        self.entry_time = time.fromisoformat(params["entry_time"])
        self.exit_time = time.fromisoformat(params["exit_time"])
        self.entry_window = timedelta(minutes=int(params.get("entry_window_minutes", 5)))
        self.sl_pct = float(params["stop_loss_pct"]) / 100
        self.by_weekday: dict[int, tuple[str, dict]] = {}
        for underlying, cfg in params["schedule"].items():
            for day in cfg["days"]:
                if WEEKDAYS[day] in self.by_weekday:
                    raise ValueError(f"{day} is scheduled for more than one underlying")
                self.by_weekday[WEEKDAYS[day]] = (underlying, cfg)
        self._day: date | None = None

    def _reset_day(self, today: date) -> None:
        self._day = today
        self.state = "IDLE"  # IDLE -> OPEN -> DONE
        self.today = self.by_weekday.get(today.weekday())  # (underlying, cfg) or None
        self.hedges: dict[OptionType, Instrument] = {}
        self.lots = 0

    # ---- lifecycle ------------------------------------------------------

    def on_bar(self, ctx: Context) -> None:
        clock = ctx.data.bar_end()
        if clock.date() != self._day:
            self._reset_day(clock.date())
        if self.today is None or self.state == "DONE":
            return

        if self.state == "IDLE" and self.entry_time <= clock.time() < self.exit_time:
            if clock - datetime.combine(clock.date(), self.entry_time) > self.entry_window:
                self.state = "DONE"  # session started late (e.g. Muhurat trading): no trade today
                return
            self._enter(ctx)
        elif self.state == "OPEN" and clock.time() >= self.exit_time:
            ctx.cancel_all()
            ctx.square_off_all(tag="exit-time")
            self.state = "DONE"

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        kind, _, side = fill.tag.partition(":")
        if kind == "short":
            trigger = round(fill.price * (1 + self.sl_pct), 2)
            ctx.stop_buy(fill.instrument, self.lots, trigger, tag=f"sl:{side}")
        elif kind == "sl":
            ctx.sell(self.hedges[OptionType(side)], self.lots, tag=f"hedge-exit:{side}")

    # ---- helpers --------------------------------------------------------

    def _enter(self, ctx: Context) -> None:
        underlying, cfg = self.today
        self.state = "DONE"  # unless every leg is found below

        spot = ctx.data.spot(underlying)
        expiries = ctx.data.expiries(underlying)
        if spot is None or not expiries:
            log.warning("%s %s: no spot/expiry data, skipping day", self._day, underlying)
            return
        expiry = expiries[0]
        chain = ctx.data.option_chain(underlying, expiry)
        if not chain:
            log.warning("%s %s: empty option chain for %s, skipping day", self._day, underlying, expiry)
            return

        atm = min({i.strike for i in chain}, key=lambda s: abs(s - spot))
        dist = cfg["hedge_distance"]
        legs = {
            "short_ce": ctx.data.find_option(underlying, expiry, atm, OptionType.CE),
            "short_pe": ctx.data.find_option(underlying, expiry, atm, OptionType.PE),
            "hedge_ce": ctx.data.find_option(underlying, expiry, atm + dist, OptionType.CE),
            "hedge_pe": ctx.data.find_option(underlying, expiry, atm - dist, OptionType.PE),
        }
        missing = [k for k, v in legs.items() if v is None]
        if missing:
            log.warning("%s %s: strikes missing %s (atm %s), skipping day", self._day, underlying, missing, atm)
            return

        self.lots = int(cfg["lots"])
        self.hedges = {OptionType.CE: legs["hedge_ce"], OptionType.PE: legs["hedge_pe"]}
        # Hedges first so the shorts get margin benefit when this runs live.
        ctx.buy(legs["hedge_ce"], self.lots, tag="hedge:CE")
        ctx.buy(legs["hedge_pe"], self.lots, tag="hedge:PE")
        ctx.sell(legs["short_ce"], self.lots, tag="short:CE")
        ctx.sell(legs["short_pe"], self.lots, tag="short:PE")
        self.state = "OPEN"
        log.info("%s %s: entered fly atm=%s spot=%.2f expiry=%s", self._day, underlying, atm, spot, expiry)
