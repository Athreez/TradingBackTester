"""Paper trading bot: live INDmoney 1-minute candles + SimBroker, same engine step as backtests.

Usage: python -m paper_trader.runner          (runs every trading day until stopped with Ctrl+C)
       python -m paper_trader.runner --once   (exit after today's session)

Writes to paper_trader/logs/ (git-ignored):
  fills.csv    every simulated fill        daily.csv   one row per trading day
  equity.csv   equity after every bar      paper_YYYYMMDD.log
State is saved to paper_trader/state/ after every bar, so a restart resumes
the day (open positions, pending stop-losses) instead of entering twice.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import logging
import os
import pickle
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from backtester.engine import run_bar, settle_expiring
from backtester.historical_data import HistoricalMarketData
from backtester.sim_broker import SimBroker
from core import models
from core.config import env, load_env
from core.costs import CostModel
from core.logsafe import protect_handlers
from core.models import Side
from core.registry import load_strategy
from core.strategy_base import Context
from data.indmoney_auth import AuthError, LoginConflict
from data.indmoney_client import IndMoneyClient
from paper_trader.live_feed import LiveFeed, MemoryStore

HERE = Path(__file__).parent
LOG_DIR = HERE / "logs"
STATE_PATH = HERE / "state" / "state.pkl"
SNAPSHOT_PATH = HERE / "state" / "live.json"  # read by paper_trader/dashboard.py
IST = timezone(timedelta(hours=5, minutes=30))
SESSION_START, SESSION_END = time(9, 15), time(15, 30)
PREPARE_AT, CLOSE_AT = time(9, 5), time(15, 35)
BAR = timedelta(minutes=1)
GRACE = timedelta(seconds=5)  # wait after a minute closes so the candle is published
POLL_SECONDS = 5
AUTH_BACKOFF_SECONDS = 65  # INDmoney allows one token per minute
LOGIN_BACKOFF_SECONDS = 120
MIRROR_OFF = "turned off with /mirror off"

log = logging.getLogger("paper")


def _single_instance_lock():
    """Exit if another paper bot is already running (two bots would overwrite each other's state)."""
    lock_path = STATE_PATH.parent / "bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("Another paper bot is already running (paper_trader/state/bot.lock).")
    return f  # keep the handle open for the life of the process


def ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _pause(seconds: float) -> None:
    _time.sleep(seconds)


def _sleep_until(target: datetime) -> None:
    while (left := (target - ist_now()).total_seconds()) > 0:
        _pause(min(left, 60))


def _next_session_day(after: date) -> date:
    d = after + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _replace(tmp: Path, target: Path) -> bool:
    """Atomically swap tmp into target. On Windows a reader (dashboard, antivirus, search indexer) can hold
    target open for a moment, so retry briefly; on failure log and let the next write try again."""
    for _ in range(20):
        try:
            tmp.replace(target)
            return True
        except PermissionError:
            _time.sleep(0.05)
    log.warning("could not update %s (file in use); the next write tries again", target.name)
    return False


def _append_csv(path: Path, row: dict) -> None:
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


class PaperBot:
    def __init__(self, strategy_name: str, capital: float | None):
        self.strategy_name = strategy_name
        self.strategy, self.config = load_strategy(strategy_name)
        self.interval = self.config.get("interval", "1minute")
        self.underlyings = self.config["underlyings"]
        self.costs = CostModel.from_dict(self.config.get("costs"))
        self.capital = capital or float(self.config.get("capital", 500_000))
        self.client = IndMoneyClient.from_env()
        self.broker: SimBroker | None = None
        self.last_bar: datetime | None = None
        self.day: date | None = None
        self.day_closed = False
        self.state_path, self.snapshot_path, self.log_dir = self._paths()
        self._restore()
        self._fills_logged = len(self.broker.fills())
        from paper_trader.commands import Controls
        from paper_trader.notify import Telegram, TradeNotifier
        self.controls = Controls(self.state_path.parent / "controls.json")  # set by Telegram commands
        self.tg = Telegram.from_env()  # TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS in .env, else off
        self.notifier = TradeNotifier(self.tg, muted=lambda: self.controls.get("muted")) if self.tg else None
        self.mirror = None  # live_trader.mirror.OrderMirror for the current day, if on

    def start_commands(self) -> None:
        """Answer Telegram commands (/help) from a background thread."""
        if self.tg is None:
            return
        from paper_trader.commands import CommandBot, CommandHandler, Paths
        CommandBot(self.tg, CommandHandler(self.controls, Paths(self.snapshot_path, self.log_dir, HERE.parent / "live_trader"),
                                           now=ist_now), blocked_path=self.state_path.parent / "blocked_chats.json").start()

    # ---- hooks for live_trader.runner.LiveBot (no-ops on paper) -------------

    def _paths(self) -> tuple[Path, Path, Path]:
        """state.pkl, live.json (dashboard / commands), logs folder."""
        return STATE_PATH, SNAPSHOT_PATH, LOG_DIR

    def _new_broker(self) -> SimBroker:
        return SimBroker(lambda i: None, self.costs, self.capital)

    def _broker_class(self) -> type:
        return SimBroker

    def _start_broker_day(self, feed: LiveFeed, data: HistoricalMarketData, today: date) -> None:
        pass

    def _before_bar(self, live: bool) -> None:
        pass

    def _sync_broker(self, ctx: Context) -> None:
        pass

    def _before_close(self, ctx: Context) -> None:
        pass

    def _snapshot_extra(self) -> dict:
        return {"mode": "paper"}

    # ---- state ----------------------------------------------------------

    def _restore(self) -> None:
        if not self.state_path.exists():
            self.broker = self._new_broker()
            log.info("New paper account: capital %.0f, strategy %s", self.capital, self.strategy_name)
            return
        state = pickle.loads(self.state_path.read_bytes())
        if state["strategy_name"] != self.strategy_name:
            raise SystemExit(f"Saved state is for strategy '{state['strategy_name']}'. Move {self.state_path} "
                             "away to start a fresh paper account for this strategy.")
        cls = self._broker_class()
        self.broker = cls.__new__(cls)
        self.broker.__dict__.update(state["broker"])
        if state["day"] == ist_now().date():
            # Mid-day restart: keep today's legs / SL state. Attributes added to the strategy since the
            # state was saved come from the freshly loaded instance.
            fresh, self.strategy = self.strategy, state["strategy"]
            for k, v in fresh.__dict__.items():
                self.strategy.__dict__.setdefault(k, v)
        self.last_bar, self.day, self.day_closed = state["last_bar"], state["day"], state["day_closed"]
        # Keep order ids unique across restarts.
        models._order_ids = itertools.count(max((o.id for o in self.broker.orders()), default=0) + 1)
        log.info("Resumed paper account: cash %.2f, open positions %d, last bar %s",
                 self.broker.cash, len(self.broker.positions()), self.last_bar)

    def _save(self) -> None:
        save_state(self.state_path, self.strategy_name, self.broker, self.strategy, self.last_bar, self.day, self.day_closed)

    # ---- day lifecycle --------------------------------------------------

    def run(self, once: bool = False) -> None:
        while True:
            today = ist_now().date()
            if today.weekday() >= 5 or (self.day == today and self.day_closed):
                if once and self.day == today:
                    return
                nxt = _next_session_day(today)
                log.info("Next session %s; sleeping", nxt)
                self._write_snapshot(None, f"idle until next session {nxt}")
                _sleep_until(datetime.combine(nxt, PREPARE_AT))
                continue
            if ist_now() < datetime.combine(today, PREPARE_AT):
                self._write_snapshot(None, f"waiting for session start ({PREPARE_AT:%H:%M})")
            _sleep_until(datetime.combine(today, PREPARE_AT))
            self._run_day(today)
            if once:
                return

    def _run_day(self, today: date) -> None:
        if self.day != today:
            self.day, self.day_closed = today, False
            # A new day starts from a fresh strategy, not one restored from an earlier day's state.
            self.strategy, self.config = load_strategy(self.strategy_name)
        self._setup_logfile(today)
        log.info("=== %s: preparing session", today)

        store = MemoryStore()
        feed = LiveFeed(self.client, store, self.underlyings, interval=self.interval)
        while True:  # usually the network (e.g. switching Wi-Fi): keep retrying until it's back
            try:
                feed.load_contracts(today)
                break
            except Exception as e:
                if ist_now() >= datetime.combine(today, CLOSE_AT):
                    log.error("Could not load contracts before the session closed: %s", e)
                    return
                log.warning("loading contracts failed (%s); retrying in 30s", e)
                self._write_snapshot(None, f"loading contracts failed, retrying: {e}")
                _pause(30)
        data = HistoricalMarketData(store, self.interval)
        self.broker._quote = lambda inst: data.bar_at(inst, data.now())
        ctx = Context(data, self.broker)
        session_open = datetime.combine(today, SESSION_START)
        self.mirror = self._make_mirror(feed, today)
        self._start_broker_day(feed, data, today)

        while ist_now() < datetime.combine(today, CLOSE_AT):
            self._apply_controls(ctx, feed, today)
            now = ist_now()
            symbols = sorted({i.symbol for i in self.broker.positions()}
                             | {o.instrument.symbol for o in self.broker.open_orders()})
            fetch_from = max(session_open, (self.last_bar or session_open) - 3 * BAR)
            try:
                feed.poll(symbols, fetch_from, now)
            except LoginConflict as e:  # someone else holds the login: don't fight over tokens
                log.error("%s", e)
                self._write_snapshot(data, f"login problem: {e}")
                _pause(LOGIN_BACKOFF_SECONDS)
                continue
            except AuthError as e:  # e.g. token generation throttled: wait instead of hammering
                log.warning("login failed, retrying in %ss: %s", AUTH_BACKOFF_SECONDS, e)
                self._write_snapshot(data, f"login problem: {e}")
                _pause(AUTH_BACKOFF_SECONDS)
                continue
            except Exception as e:  # network hiccup: keep the bot alive, retry next poll
                log.warning("poll failed: %s", e)
                _pause(POLL_SECONDS)
                continue
            data._frames.clear()
            self._sync_broker(ctx)

            completed = now - BAR - GRACE  # bars with ts <= this have closed and been published
            after = self.last_bar if self.last_bar and self.last_bar.date() == today else None
            for ts in feed.index_bars(after, completed):
                live = completed - ts < 2 * BAR  # older bars are catch-up after a late start
                self._before_bar(live)
                run_bar(self.strategy, ctx, self.broker, data, ts)
                self.last_bar = ts
                self._sync_broker(ctx)
                self._log_new_fills(catch_up=not live)
                if self.mirror:
                    self.mirror.sync(self.broker, ts, live=live)
                _append_csv(self.log_dir / "equity.csv", {"ts": ts, "equity": round(self.broker.equity(data.ltp), 2)})
                self._save()
            if self.mirror:
                self.mirror.poll()
            self._write_snapshot(data, "trading session")
            _pause(POLL_SECONDS)

        self._before_close(ctx)
        row = close_day(self.strategy, ctx, self.broker, data, today, self.last_bar, self.log_dir)
        if self.mirror:
            self.mirror.sync(self.broker, self.last_bar, live=False)
            self.mirror.close_day()
        self._log_new_fills()
        if self.notifier and (self.last_bar and self.last_bar.date() == today):  # not on market holidays
            try:
                self.notifier.day_summary(today, row, self.strategy, self.broker.initial_capital,
                                          self.mirror.summary() if self.mirror else "")
            except Exception as e:
                log.warning("telegram day summary failed: %s", e)
        self.day_closed = True
        self._save()
        self._write_snapshot(data, "session closed")

    def _mirror_wanted(self) -> bool:
        """/mirror on|off from Telegram overrides LIVE_ORDERS in .env."""
        override = self.controls.get("mirror")
        return override if override is not None else env("LIVE_ORDERS", "off") == "mirror"

    def _apply_controls(self, ctx: Context, feed: LiveFeed, today: date) -> None:
        """Act on Telegram commands: /pause, /mirror, /exitnow (set from another thread)."""
        self.strategy.paused = self.controls.get("paused")  # checked by the strategy at entry time
        wanted = self._mirror_wanted()
        if wanted and self.mirror is None:
            self.mirror = self._make_mirror(feed, today)
        elif self.mirror is not None:
            if not wanted and not self.mirror.halted:
                self.mirror.halted = MIRROR_OFF
            elif wanted and self.mirror.halted == MIRROR_OFF:
                self.mirror.halted = None  # safety stops (balance, fills, cap) are never undone here
        if self.controls.exit_requested.is_set():
            self.controls.exit_requested.clear()
            had = bool(self.broker.positions() or self.broker.open_orders())
            ctx.cancel_all()
            ctx.square_off_all(tag="manual-exit")
            self.strategy.state = "DONE"  # no (re-)entry for the rest of the day
            self.strategy.note = (getattr(self.strategy, "note", "") or "") + "; closed via /exitnow"
            log.warning("/exitnow from Telegram: %s", "closing all positions" if had else "nothing was open")
            if self.notifier and not had:
                self.notifier.alert("/exitnow: nothing was open; no entry for the rest of today")

    def _make_mirror(self, feed: LiveFeed, today: date):
        """Also send every order to INDmoney (see live_trader/mirror.py) when LIVE_ORDERS=mirror or /mirror on."""
        if not self._mirror_wanted():
            return None
        from live_trader.mirror import OrderMirror
        mirror = OrderMirror(self.client, feed._symbol_to_code, today, HERE.parent / "live_trader",
                             max_balance=float(env("LIVE_MAX_BALANCE", "100")), notifier=self.notifier)
        mirror.start(self.broker)
        log.info("Live order mirroring ON%s", f" but halted: {mirror.halted}" if mirror.halted else "")
        return mirror

    def _write_snapshot(self, data: HistoricalMarketData | None, phase: str) -> None:
        mirror = ("off" if self.mirror is None and not self._mirror_wanted() else
                  "on (starts with the session)" if self.mirror is None else
                  f"stopped: {self.mirror.halted}" if self.mirror.halted else "on")
        write_snapshot(self.snapshot_path, phase=phase, strategy_name=self.strategy_name, broker=self.broker,
                       strategy=self.strategy, data=data, day=self.day, last_bar=self.last_bar,
                       extra={"paused": self.controls.get("paused"), "mirror": mirror, "plan": self._plan(data),
                              **self._snapshot_extra()})

    def _plan(self, data: HistoricalMarketData | None) -> dict | None:
        """Today's plan for /today: underlying, expiry, days to it, VIX now and the rule it gives."""
        s = self.strategy
        today = getattr(s, "today", None)
        if not today or data is None or data._now is None or self.day is None:
            return None
        try:
            underlying = today[0]
            expiry = min(e for e in data.expiries(underlying) if e >= self.day)
            vix = data.index_value("INDIAVIX")
            rule = None
            regimes = getattr(s, "vix_regimes", None)
            if vix is not None and regimes:
                from strategies.vix_iron_fly.strategy import _matches
                hit = next((r for r in regimes if _matches(r, vix)), None)
                rule = f"trade, stop-loss {hit['stop_loss_pct']}%" if hit else "no trade (VIX in the no-trade band)"
            return {"underlying": underlying, "expiry": str(expiry), "dte": (expiry - self.day).days,
                    "vix": vix, "rule": rule, "max_dte": getattr(s, "max_dte", None),
                    "entry_time": str(getattr(s, "entry_time", ""))[:5], "exit_time": str(getattr(s, "exit_time", ""))[:5]}
        except Exception as e:  # the plan is informational only
            log.debug("plan unavailable: %s", e)
            return None

    def _log_new_fills(self, catch_up: bool = False) -> None:
        new = self.broker.fills()[self._fills_logged:]
        log_fills(new, self.log_dir)
        self._fills_logged = len(self.broker.fills())
        if self.notifier and new:
            try:
                self.notifier.fills(new, self.broker.fills(), self.strategy, catch_up=catch_up)
            except Exception as e:  # notifications must never break the bot
                log.warning("telegram notification failed: %s", e)

    def _setup_logfile(self, today: date) -> None:
        setup_day_logfile(self.log_dir, today)


# ---- shared with paper_trader/simulate.py (same journals and snapshot) ----

def save_state(path: Path, strategy_name: str, broker: SimBroker, strategy, last_bar: datetime | None,
               day: date | None, day_closed: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    transient = getattr(broker, "TRANSIENT", ("_quote",))  # live connections etc. are re-attached on restore
    broker_state = {k: v for k, v in broker.__dict__.items() if k not in transient}
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps({
        "strategy_name": strategy_name, "broker": broker_state, "strategy": strategy,
        "last_bar": last_bar, "day": day, "day_closed": day_closed,
    }))
    _replace(tmp, path)


def log_fills(fills, log_dir: Path) -> None:
    for f in fills:
        log.info("FILL %s %s %d @ %.2f (charges %.2f) [%s]", f.side.value, f.instrument.symbol,
                 f.quantity, f.price, f.charges, f.tag)
        _append_csv(log_dir / "fills.csv", {
            "ts": f.ts, "symbol": f.instrument.symbol, "side": f.side.value, "qty": f.quantity,
            "price": round(f.price, 2), "charges": round(f.charges, 2), "tag": f.tag})


def setup_day_logfile(log_dir: Path, day: date, fmt: logging.Formatter | None = None, prefix: str = "paper") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
        root.removeHandler(h)
        h.close()
    fh = logging.FileHandler(log_dir / f"{prefix}_{day:%Y%m%d}.log", encoding="utf-8")
    fh.setFormatter(fmt or logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    protect_handlers(root)  # mask tokens and secrets in every log line


def close_day(strategy, ctx: Context, broker: SimBroker, data: HistoricalMarketData, day: date,
              last_bar: datetime | None, log_dir: Path) -> dict:
    """Settle anything expiring today, append the day's row to daily.csv and return it."""
    if not (last_bar and last_bar.date() == day) and not any(f.ts.date() == day for f in broker.fills()):
        log.info("=== %s: no candles all day (market holiday?), no daily row", day)
        return {"date": day, "fills": 0, "gross": 0, "charges": 0, "net": 0,
                "equity": round(broker.equity(lambda inst: None), 2)}  # no candles: positions at cost
    if last_bar and last_bar.date() == day:
        data.set_time(last_bar)
        for fill in settle_expiring(broker, data, last_bar):
            strategy.on_fill(ctx, fill)
    if broker.positions():
        log.warning("Positions still open after close: %s",
                    {i.symbol: p.quantity for i, p in broker.positions().items()})
    day_fills = [f for f in broker.fills() if f.ts.date() == day]
    gross = sum((f.quantity * f.price) * (1 if f.side is Side.SELL else -1) for f in day_fills)
    charges = sum(f.charges for f in day_fills)
    row = {"date": day, "fills": len(day_fills), "gross": round(gross, 2), "charges": round(charges, 2),
           "net": round(gross - charges, 2), "equity": round(broker.equity(data.ltp), 2)}
    _append_csv(log_dir / "daily.csv", row)
    log.info("=== %s closed: %s", day, row)
    return row


def write_snapshot(path: Path, *, phase: str, strategy_name: str, broker: SimBroker, strategy,
                   data: HistoricalMarketData | None, day: date | None, last_bar: datetime | None,
                   extra: dict | None = None) -> None:
    """Small JSON for the dashboard: positions with last prices, pending stops, day status."""
    from data.indmoney_auth import CACHE_PATH, _read_cache

    def ltp(inst):
        if data is None or data._now is None:
            return None
        return data.ltp(inst)

    positions = []
    for inst, pos in broker.positions().items():
        last = ltp(inst)
        positions.append({"symbol": inst.symbol, "qty": pos.quantity, "avg_price": round(pos.avg_price, 2),
                          "ltp": last, "unrealized": round(pos.unrealized_pnl(last), 2) if last else None})
    orders = [{"symbol": o.instrument.symbol, "side": o.side.value, "type": o.order_type.value,
               "qty": o.quantity, "trigger": o.trigger_price, "limit": o.limit_price, "tag": o.tag}
              for o in broker.open_orders()]
    today = getattr(strategy, "today", None)
    token = _read_cache(CACHE_PATH)
    snap = {
        "updated_at": ist_now().isoformat(timespec="seconds"),
        "phase": phase,
        "strategy": strategy_name,
        "day": str(day) if day else None,
        "last_bar": last_bar.isoformat() if last_bar else None,
        "underlying": today[0] if today else None,
        "spot": (data.spot(today[0]) if data is not None and data._now is not None and today else None),
        "vix_at_entry": getattr(strategy, "day_vix", None),
        "day_sl_pct": getattr(strategy, "day_sl_pct", None),
        "state": getattr(strategy, "state", None),
        "note": getattr(strategy, "note", None),
        "cash": round(broker.cash, 2),
        "equity": round(broker.equity(ltp if data is not None else (lambda i: None)), 2),
        "initial_capital": broker.initial_capital,
        "positions": positions,
        "open_orders": orders,
        "token_valid_until": token[1].isoformat(timespec="minutes") if token else None,
        **(extra or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=2, default=str))
    _replace(tmp, path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="exit after today's session")
    args = p.parse_args()

    load_env()
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    protect_handlers()
    name = env("STRATEGY")
    if not name:
        raise SystemExit("Set STRATEGY in .env (see .env.example)")
    capital = float(env("CAPITAL")) if env("CAPITAL") else None
    lock = _single_instance_lock()  # noqa: F841 (held until exit)
    bot = PaperBot(name, capital)
    bot.start_commands()
    bot.run(once=args.once)


if __name__ == "__main__":
    main()
