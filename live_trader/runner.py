"""Real-money trading bot: the paper bot's loop with a LiveBroker (live_trader/broker.py) placing INDmoney orders.

Usage: python -m live_trader.runner          (needs LIVE_TRADING_ENABLED=yes in .env)
       python -m live_trader.runner --once   (exit after today's session)

Same strategy, candle feed, Telegram messages and commands as the paper bot. Journals go to
live_trader/logs/ (fills.csv, daily.csv, equity.csv, orders.csv, events.csv, live_YYYYMMDD.log) and state
to live_trader/state/. It takes the paper bot's lock, so the two never run at once: they would share (and
fight over) one INDmoney login.

.env: LIVE_MAX_LOTS (default 1) caps every order; LIVE_MAX_ENTRY_ORDERS (default 4) caps entry orders a day
(stops, exits and retries are never blocked).
"""
from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from core.config import env, load_env
from core.logsafe import protect_handlers
from core.strategy_base import Context
from live_trader.broker import LiveBroker
from paper_trader import runner as paper
from paper_trader.runner import PaperBot, ist_now, setup_day_logfile

HERE = Path(__file__).parent
CLOSE_POLLS = 6  # day end: polls (POLL_SECONDS apart) waiting for INDmoney to confirm the cancels

log = logging.getLogger("live")


class LiveBot(PaperBot):
    broker: LiveBroker

    def __init__(self, strategy_name: str, capital: float | None):
        super().__init__(strategy_name, capital)
        self.broker.attach(self.client, {}, ist_now)
        self.broker._quote = lambda inst: None  # set to the day's candles in _run_day

    def _paths(self):
        return HERE / "state" / "state.pkl", HERE / "state" / "live.json", HERE / "logs"

    def _new_broker(self) -> LiveBroker:
        log.info("New LIVE account record: capital %.0f, strategy %s", self.capital, self.strategy_name)
        return LiveBroker(self.client, {}, self.costs, self.capital, self.log_dir, now=ist_now,
                          max_lots=int(env("LIVE_MAX_LOTS", "1")), max_entries=int(env("LIVE_MAX_ENTRY_ORDERS", "4")))

    def _broker_class(self) -> type:
        return LiveBroker

    def _mirror_wanted(self) -> bool:
        return False  # the orders are real: nothing to mirror

    def _setup_logfile(self, today: date) -> None:
        setup_day_logfile(self.log_dir, today, prefix="live")

    def _snapshot_extra(self) -> dict:
        b = self.broker
        return {"mode": "live", "mirror": "not used (live trading)", "halted": b.halted,
                "live_orders": [{"tag": b._by_id[s.order_id].tag, "symbol": b._by_id[s.order_id].instrument.symbol,
                                 "side": b._by_id[s.order_id].side.value, "qty": s.qty, "kind": s.kind,
                                 "filled": s.filled, "price": s.avg_price, "done": s.done,
                                 "status": ", ".join(s.statuses.values()) or ("unknown" if s.unknown else "sent")}
                                for s in b.sent.values() if b.day and s.sent_at.date() == b.day]}

    # ---- broker hooks ---------------------------------------------------

    def _start_broker_day(self, feed, data, today: date) -> None:
        """Before anything is traded: read INDmoney's orders and positions, book what happened while the bot
        was away (stops that fired, legs you closed, orders you cancelled), and say what it will do next."""
        self.broker.attach(self.client, feed._symbol_to_code, ist_now)
        self.broker.start_day(today)
        self.broker.poll()  # order book first, then the positions check (acts at once after a start)
        self._drain_alerts()
        now = ist_now().time()
        if self.notifier and paper.SESSION_START <= now < paper.SESSION_END:  # a restart during the session
            self.notifier.alert(self._state_report())

    def _state_report(self) -> str:
        b, s = self.broker, self.strategy
        legs = ", ".join(f"{i.symbol} {p.quantity:+d}" for i, p in b.positions().items()) or "none"
        stops = ", ".join(f"{o.instrument.symbol} @ {o.trigger_price:.2f}" for o in b.open_orders()
                          if o.trigger_price is not None) or "none"
        bare = [i.symbol for i, p in b.positions().items() if p.quantity < 0 and not any(
            o.instrument == i and o.trigger_price is not None for o in b.open_orders())]
        state = getattr(s, "state", "IDLE")
        if b.halted:
            nxt = f"no new entries today ({b.halted})"
        elif state == "OPEN" or b.positions():
            nxt = f"managing the open legs, exit at {s.exit_time:%H:%M}"
        elif state == "DONE":
            nxt = "done for today"
        else:
            nxt = f"entry at {s.entry_time:%H:%M} if VIX allows (not if that time has passed)"
        return (f"Checked INDmoney. Open legs: {legs}. Stop-losses: {stops}."
                + (f" ⚠️ No stop-loss on: {', '.join(bare)}." if bare else "") + f" Next: {nxt}.")

    def _before_bar(self, live: bool) -> None:
        self.broker.accept_opening = live  # never open positions from bars replayed after a late start

    def _sync_broker(self, ctx: Context) -> None:
        """Every poll and after every bar: read INDmoney, hand fills to the strategy, send what is queued."""
        b = self.broker
        b.poll()
        if ctx.data._now is None:  # no bar yet (just restarted): fills wait for the first bar, which hands
            b.send_pending()       # them to the strategy through run_bar with the clock set
            self._drain_alerts()
            self._save()
            return
        for fill in b.take_fills():
            self.strategy.on_fill(ctx, fill)
        if b.entry_failed:
            self._abandon_entry(ctx)
        b.send_pending()
        self._log_new_fills()
        self._drain_alerts()
        self._save()

    def _abandon_entry(self, ctx: Context) -> None:
        """An entry leg failed: close whatever did fill and stop for the day (never hold an unhedged fly)."""
        if self.strategy.state != "DONE":
            self.strategy.state = "DONE"
            self.strategy.note = (getattr(self.strategy, "note", "") or "") + f"; entry abandoned: {self.broker.entry_failed}"
            log.error("entry abandoned: %s", self.broker.entry_failed)
            if self.notifier:
                self.notifier.alert(f"Entry abandoned: {self.broker.entry_failed}. Closing what filled.")
        ctx.cancel_all()
        if self.broker.positions() and not self.broker._pending:
            ctx.square_off_all(tag="entry-failed")

    def _before_close(self, ctx: Context) -> None:
        b = self.broker
        b.cancel_everything()  # GTT stops would otherwise stay live on INDmoney for a year
        for _ in range(CLOSE_POLLS):
            b.poll()
            if not b.working():
                break
            paper._pause(paper.POLL_SECONDS)
        for fill in b.take_fills():
            self.strategy.on_fill(ctx, fill)
        b.reconcile()
        if b.working():
            b._alert("Orders still open on INDmoney after the close: " + ", ".join(
                f"{b._by_id[s.order_id].tag} {b._by_id[s.order_id].instrument.symbol}" for s in b.working())
                + ". Cancel them in the INDmoney app.")
        if b.positions():
            b._alert("Positions still open after the close: " + ", ".join(
                f"{i.symbol} {p.quantity}" for i, p in b.positions().items()) + ". Check the INDmoney app.")
        self._log_new_fills()
        self._drain_alerts()

    def _drain_alerts(self) -> None:
        alerts, self.broker.alerts = self.broker.alerts, []
        for text in alerts:
            if self.notifier:
                self.notifier.alert(f"LIVE: {text}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="exit after today's session")
    args = p.parse_args()

    load_env()
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    protect_handlers()
    if env("LIVE_TRADING_ENABLED") != "yes":
        raise SystemExit("LIVE_TRADING_ENABLED is not 'yes' in .env -- refusing to trade real money.")
    name = env("STRATEGY")
    if not name:
        raise SystemExit("Set STRATEGY in .env (see .env.example)")
    capital = float(env("CAPITAL")) if env("CAPITAL") else None
    lock = paper._single_instance_lock()  # noqa: F841 (same lock as the paper bot: one INDmoney login)
    bot = LiveBot(name, capital)
    bot.start_commands()
    bot.announce_start()
    if bot.notifier:
        bot.notifier.alert(f"LIVE bot started ({name}, max {bot.broker.max_lots} lot per order). Real money.")
    bot.run(once=args.once)


if __name__ == "__main__":
    main()
