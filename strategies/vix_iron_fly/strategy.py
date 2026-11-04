"""Intraday iron fly on the nearest weekly expiry, gated by India VIX (see README.md).

At entry_time: buy hedges at ATM +/- hedge_distance, sell ATM CE + PE.
Each short leg gets a stop-loss at entry * (1 + stop_loss_pct/100). When one
fires: `exit_all` (default) squares off every remaining leg and stops for the
day; `exit_side` only sells that side's hedge. Anything left is squared off at exit_time.
Each day trades the `schedule` underlying whose weekly expiry comes soonest (ties: first listed), so the
choice follows the exchanges' expiry weekdays as they change (NIFTY Tue / SENSEX Thu since Sep 2025).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from core.models import Fill, Instrument, OptionType
from core.strategy_base import Context, Strategy

log = logging.getLogger(__name__)


def _matches(regime: dict, vix: float) -> bool:
    below, above = regime.get("vix_below"), regime.get("vix_above")
    return (below is None or vix < below) and (above is None or vix > above)


class VixIronFly(Strategy):
    def __init__(self, params):
        super().__init__(params)
        self.entry_time = time.fromisoformat(params["entry_time"])
        self.exit_time = time.fromisoformat(params["exit_time"])
        self.entry_window = timedelta(minutes=int(params.get("entry_window_minutes", 5)))
        self.sl_pct = float(params["stop_loss_pct"]) / 100
        self.skip_expiry_day = bool(params.get("skip_expiry_day", False))
        self.vix_min = params.get("vix_min")  # trade only if India VIX at entry is within [vix_min, vix_max]
        self.vix_max = params.get("vix_max")
        # Optional VIX regimes: first match sets the day's SL; no match -> no trade.
        # e.g. [{vix_below: 11, stop_loss_pct: 40}, {vix_above: 14, stop_loss_pct: 30}]
        self.vix_regimes = params.get("vix_regimes") or []
        self.on_stop_loss = params.get("on_stop_loss", "exit_all")
        if self.on_stop_loss not in ("exit_all", "exit_side"):
            raise ValueError("on_stop_loss must be 'exit_all' or 'exit_side'")
        self.schedule: dict[str, dict] = params["schedule"]
        self._day: date | None = None

    def _pick_underlying(self, ctx: Context, today: date) -> tuple[str, dict] | None:
        """The scheduled underlying with the soonest live expiry."""
        best = None
        for underlying, cfg in self.schedule.items():
            expiries = [e for e in ctx.data.expiries(underlying) if e >= today]
            if expiries and (best is None or expiries[0] < best[0]):
                best = (expiries[0], underlying, cfg)
        return best and (best[1], best[2])

    def _reset_day(self, ctx: Context, today: date) -> None:
        self._day = today
        self.state = "IDLE"  # IDLE -> OPEN -> DONE
        self.today = self._pick_underlying(ctx, today)  # (underlying, cfg) or None
        self.hedges: dict[OptionType, Instrument] = {}
        self.lots = 0
        self.day_sl_pct = self.sl_pct
        self.day_vix: float | None = None
        self.note = "no expiry data for any underlying" if self.today is None else "waiting for entry time"

    # ---- lifecycle ------------------------------------------------------

    def on_bar(self, ctx: Context) -> None:
        clock = ctx.data.bar_end()
        if clock.date() != self._day:
            self._reset_day(ctx, clock.date())
        if self.today is None or self.state == "DONE":
            return

        if self.state == "IDLE" and self.entry_time <= clock.time() < self.exit_time:
            if clock - datetime.combine(clock.date(), self.entry_time) > self.entry_window:
                self.state = "DONE"  # session started late (e.g. Muhurat trading): no trade today
                self.note = "session started after the entry window, no trade"
                return
            self._enter(ctx)
        elif self.state == "OPEN" and clock.time() >= self.exit_time:
            ctx.cancel_all()
            ctx.square_off_all(tag="exit-time")
            self.state = "DONE"
            self.note += f"; exited at {clock:%H:%M}"

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        kind, _, side = fill.tag.partition(":")
        if kind == "short":
            trigger = round(fill.price * (1 + self.day_sl_pct), 2)
            ctx.stop_buy(fill.instrument, self.lots, trigger, tag=f"sl:{side}")
        elif kind == "sl" and self.state == "OPEN":  # a 2nd SL in the same bar is already handled
            self.note += f"; {side} stop-loss hit at {fill.ts:%H:%M}"
            if self.on_stop_loss == "exit_all":
                ctx.cancel_all()
                ctx.square_off_all(tag="sl-exit")
                self.state = "DONE"
            else:
                ctx.sell(self.hedges[OptionType(side)], self.lots, tag=f"hedge-exit:{side}")

    # ---- helpers --------------------------------------------------------

    def _enter(self, ctx: Context) -> None:
        underlying, cfg = self.today
        self.state = "DONE"  # unless every leg is found below

        if self.vix_min is not None or self.vix_max is not None or self.vix_regimes:
            vix = self.day_vix = ctx.data.index_value("INDIAVIX")
            if vix is None:
                log.warning("%s: no India VIX data, skipping day", self._day)
                self.note = "no India VIX data, no trade"
                return
            if (self.vix_min is not None and vix < self.vix_min) or (self.vix_max is not None and vix > self.vix_max):
                log.info("%s %s: VIX %.2f outside [%s, %s], no trade", self._day, underlying, vix,
                         self.vix_min, self.vix_max)
                self.note = f"VIX {vix:.2f} outside [{self.vix_min}, {self.vix_max}], no trade"
                return
            if self.vix_regimes:
                regime = next((r for r in self.vix_regimes if _matches(r, vix)), None)
                if regime is None:
                    log.info("%s %s: VIX %.2f matches no regime, no trade", self._day, underlying, vix)
                    self.note = f"VIX {vix:.2f} is in the no-trade band, no trade"
                    return
                self.day_sl_pct = float(regime["stop_loss_pct"]) / 100

        spot = ctx.data.spot(underlying)
        expiries = ctx.data.expiries(underlying)
        if spot is None or not expiries:
            log.warning("%s %s: no spot/expiry data, skipping day", self._day, underlying)
            self.note = "no index/expiry data, no trade"
            return
        expiry = expiries[0]
        if self.skip_expiry_day and expiry == self._day:
            log.info("%s %s: expiry day, no trade", self._day, underlying)
            self.note = "expiry day, no trade"
            return
        chain = ctx.data.option_chain(underlying, expiry)
        if not chain:
            log.warning("%s %s: empty option chain for %s, skipping day", self._day, underlying, expiry)
            self.note = "empty option chain, no trade"
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
            self.note = f"strikes missing {missing}, no trade"
            return

        self.lots = int(cfg["lots"])
        self.hedges = {OptionType.CE: legs["hedge_ce"], OptionType.PE: legs["hedge_pe"]}
        # Hedges first so the shorts get margin benefit when this runs live.
        ctx.buy(legs["hedge_ce"], self.lots, tag="hedge:CE")
        ctx.buy(legs["hedge_pe"], self.lots, tag="hedge:PE")
        ctx.sell(legs["short_ce"], self.lots, tag="short:CE")
        ctx.sell(legs["short_pe"], self.lots, tag="short:PE")
        self.state = "OPEN"
        self.note = f"entered: ATM {atm:.0f}, expiry {expiry}, SL {self.day_sl_pct:.0%}"
        log.info("%s %s: entered fly atm=%s spot=%.2f expiry=%s", self._day, underlying, atm, spot, expiry)
