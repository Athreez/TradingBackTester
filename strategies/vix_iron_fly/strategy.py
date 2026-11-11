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
from data.underlyings import get_underlying

log = logging.getLogger(__name__)


def _tick(price: float, tick: float = 0.05) -> float:
    """Round to the exchange tick size (INDmoney GTT triggers must be tick multiples)."""
    return round(round(price / tick) * tick, 2)


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
        # A hedge strike is liquid if each of the last `lookback` 1-min bars before entry traded and
        # together they traded at least `min_lots` lots; otherwise fall back to the nearest round strike.
        liq = params.get("hedge_liquidity") or {}
        self.liq_lookback = int(liq.get("lookback_minutes", 5))
        self.liq_min_lots = float(liq.get("min_lots", 0))
        self.on_stop_loss = params.get("on_stop_loss", "exit_all")
        # Stop-loss order: SL-M (`market`) or INDmoney GTT-style stop-limit (`limit`) with the limit
        # `limit_buffer_pct` above the trigger; a triggered stop still unfilled after
        # `chase_after_minutes` is cancelled and replaced by a market buy.
        stop = params.get("stop_order") or {}
        self.stop_type = stop.get("type", "market")
        if self.stop_type not in ("market", "limit"):
            raise ValueError("stop_order.type must be 'market' or 'limit'")
        self.stop_buffer = float(stop.get("limit_buffer_pct", 5)) / 100
        self.chase_after = timedelta(minutes=int(stop.get("chase_after_minutes", 1)))
        if self.on_stop_loss not in ("exit_all", "exit_side"):
            raise ValueError("on_stop_loss must be 'exit_all' or 'exit_side'")
        self.schedule: dict[str, dict] = params["schedule"]
        # Trade only when the chosen expiry is at most this many calendar days away (null = any).
        self.max_dte = params.get("max_days_to_expiry")
        self._day: date | None = None

    def _pick_underlying(self, ctx: Context, today: date) -> tuple[str, dict] | None:
        """The scheduled underlying with the soonest live expiry."""
        best = None
        for underlying, cfg in self.schedule.items():
            expiries = [e for e in ctx.data.expiries(underlying) if e >= today]
            if expiries and (best is None or expiries[0] < best[0]):
                best = (expiries[0], underlying, cfg)
        return best and (best[1], best[2])

    @staticmethod
    def _hedge_distance(underlying: str, cfg: dict, spot: float) -> float:
        """Points from ATM to each hedge: `hedge_pct` of spot rounded to the strike step (at least one
        step), or a fixed `hedge_distance`."""
        if "hedge_pct" not in cfg:
            return cfg["hedge_distance"]
        step = get_underlying(underlying).strike_step
        return max(step, round(spot * float(cfg["hedge_pct"]) / 100 / step) * step)

    def _chase_stops(self, ctx: Context, clock: datetime) -> None:
        """A stop-limit that triggered but price ran past its limit: replace it with a market buy."""
        for order in ctx.broker.open_orders():
            if order.tag.startswith("sl:") and order.triggered_at and clock - order.triggered_at >= self.chase_after:
                ctx.broker.cancel_order(order.id)
                ctx.buy(order.instrument, self.lots, tag=order.tag)
                log.info("%s: %s stop-limit unfilled since %s, chasing at market", self._day, order.instrument.symbol,
                         f"{order.triggered_at:%H:%M}")

    def _is_liquid(self, ctx: Context, inst: Instrument) -> bool:
        bars = ctx.data.history(inst, self.liq_lookback)
        bars = bars[bars.index.date == self._day]
        return (len(bars) == self.liq_lookback and bool((bars["volume"] > 0).all())
                and bars["volume"].sum() >= self.liq_min_lots * inst.lot_size)

    def _pick_hedge(self, ctx: Context, underlying: str, cfg: dict, expiry: date, atm: float,
                    target: float, option_type: OptionType) -> tuple[Instrument | None, str]:
        """The target strike if liquid, else the nearest liquid round strike (ties: nearer ATM)."""
        inst = ctx.data.find_option(underlying, expiry, target, option_type)
        if inst is None:
            return None, f"{target:.0f}{option_type.value} not listed"
        if self.liq_min_lots <= 0 or self._is_liquid(ctx, inst):
            return inst, ""
        why = f"{target:.0f}{option_type.value} illiquid"
        step = cfg.get("round_strike")
        if not step:
            return None, why
        below = target // step * step
        rounds = [k for k in (below, below + step) if k != target and (k > atm if option_type is OptionType.CE else k < atm)]
        for k in sorted(rounds, key=lambda k: (abs(k - target), abs(k - atm))):
            alt = ctx.data.find_option(underlying, expiry, k, option_type)
            if alt is not None and self._is_liquid(ctx, alt):
                return alt, f"{why}, used {k:.0f}{option_type.value}"
        return None, f"{why}, no liquid round strike"

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
            if getattr(self, "paused", False):  # set by the paper bot's Telegram /pause
                self.state = "DONE"
                self.note = "entry skipped: paused with /pause"
                return
            if clock - datetime.combine(clock.date(), self.entry_time) > self.entry_window:
                self.state = "DONE"  # session started late (e.g. Muhurat trading): no trade today
                self.note = "session started after the entry window, no trade"
                return
            self._enter(ctx)
        elif self.state == "OPEN" and clock.time() < self.exit_time:
            self._chase_stops(ctx, clock)
        elif self.state == "OPEN" and clock.time() >= self.exit_time:
            ctx.cancel_all()
            ctx.square_off_all(tag="exit-time")
            self.state = "DONE"
            self.note += f"; exited at {clock:%H:%M}"

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        kind, _, side = fill.tag.partition(":")
        if kind == "short":
            if self.stop_type == "market":
                trigger = round(fill.price * (1 + self.day_sl_pct), 2)
                ctx.stop_buy(fill.instrument, self.lots, trigger, tag=f"sl:{side}")
            else:
                trigger = _tick(fill.price * (1 + self.day_sl_pct))
                ctx.stop_buy(fill.instrument, self.lots, trigger, tag=f"sl:{side}",
                             limit=_tick(trigger * (1 + self.stop_buffer)))
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

        if self.max_dte is not None:
            dte = (min(e for e in ctx.data.expiries(underlying) if e >= self._day) - self._day).days
            if dte > self.max_dte:
                self.note = f"{underlying} expiry is {dte} days away (max {self.max_dte}), no trade"
                return

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
        dist = self._hedge_distance(underlying, cfg, spot)
        hedge_ce, why_ce = self._pick_hedge(ctx, underlying, cfg, expiry, atm, atm + dist, OptionType.CE)
        hedge_pe, why_pe = self._pick_hedge(ctx, underlying, cfg, expiry, atm, atm - dist, OptionType.PE)
        legs = {
            "short_ce": ctx.data.find_option(underlying, expiry, atm, OptionType.CE),
            "short_pe": ctx.data.find_option(underlying, expiry, atm, OptionType.PE),
            "hedge_ce": hedge_ce,
            "hedge_pe": hedge_pe,
        }
        hedge_notes = "; ".join(w for w in (why_ce, why_pe) if w)
        missing = [k for k, v in legs.items() if v is None]
        if missing:
            log.warning("%s %s: no usable strike for %s (atm %s) %s, skipping day", self._day, underlying, missing,
                        atm, hedge_notes)
            self.note = f"no usable strike for {missing}{f' ({hedge_notes})' if hedge_notes else ''}, no trade"
            return
        if hedge_notes:
            log.info("%s %s: %s", self._day, underlying, hedge_notes)

        self.lots = int(cfg["lots"])
        self.hedges = {OptionType.CE: legs["hedge_ce"], OptionType.PE: legs["hedge_pe"]}
        # Hedges first so the shorts get margin benefit when this runs live.
        ctx.buy(legs["hedge_ce"], self.lots, tag="hedge:CE")
        ctx.buy(legs["hedge_pe"], self.lots, tag="hedge:PE")
        ctx.sell(legs["short_ce"], self.lots, tag="short:CE")
        ctx.sell(legs["short_pe"], self.lots, tag="short:PE")
        self.state = "OPEN"
        self.note = f"entered: ATM {atm:.0f}, expiry {expiry}, SL {self.day_sl_pct:.0%}"
        if hedge_notes:
            self.note += f" ({hedge_notes})"
        log.info("%s %s: entered fly atm=%s spot=%.2f expiry=%s", self._day, underlying, atm, spot, expiry)
