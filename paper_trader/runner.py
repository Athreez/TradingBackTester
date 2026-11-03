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
from core.models import Side
from core.registry import load_strategy
from core.strategy_base import Context
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
        self._restore()
        self._fills_logged = len(self.broker.fills())

    # ---- state ----------------------------------------------------------

    def _restore(self) -> None:
        if not STATE_PATH.exists():
            self.broker = SimBroker(lambda i: None, self.costs, self.capital)
            log.info("New paper account: capital %.0f, strategy %s", self.capital, self.strategy_name)
            return
        state = pickle.loads(STATE_PATH.read_bytes())
        if state["strategy_name"] != self.strategy_name:
            raise SystemExit(f"Saved state is for strategy '{state['strategy_name']}'. Move {STATE_PATH} "
                             "away to start a fresh paper account for this strategy.")
        self.broker = SimBroker.__new__(SimBroker)
        self.broker.__dict__.update(state["broker"])
        if state["day"] == ist_now().date():
            self.strategy = state["strategy"]  # mid-day restart: keep today's legs / SL state
        self.last_bar, self.day, self.day_closed = state["last_bar"], state["day"], state["day_closed"]
        # Keep order ids unique across restarts.
        models._order_ids = itertools.count(max((o.id for o in self.broker.orders()), default=0) + 1)
        log.info("Resumed paper account: cash %.2f, open positions %d, last bar %s",
                 self.broker.cash, len(self.broker.positions()), self.last_bar)

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        broker_state = {k: v for k, v in self.broker.__dict__.items() if k != "_quote"}
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps({
            "strategy_name": self.strategy_name, "broker": broker_state, "strategy": self.strategy,
            "last_bar": self.last_bar, "day": self.day, "day_closed": self.day_closed,
        }))
        tmp.replace(STATE_PATH)

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

        while ist_now() < datetime.combine(today, CLOSE_AT):
            now = ist_now()
            symbols = sorted({i.symbol for i in self.broker.positions()}
                             | {o.instrument.symbol for o in self.broker.open_orders()})
            fetch_from = max(session_open, (self.last_bar or session_open) - 3 * BAR)
            try:
                feed.poll(symbols, fetch_from, now)
            except Exception as e:  # network hiccup: keep the bot alive, retry next poll
                log.warning("poll failed: %s", e)
                _pause(POLL_SECONDS)
                continue
            data._frames.clear()

            completed = now - BAR - GRACE  # bars with ts <= this have closed and been published
            after = self.last_bar if self.last_bar and self.last_bar.date() == today else None
            for ts in feed.index_bars(after, completed):
                run_bar(self.strategy, ctx, self.broker, data, ts)
                self.last_bar = ts
                self._log_new_fills()
                _append_csv(LOG_DIR / "equity.csv", {"ts": ts, "equity": round(self.broker.equity(data.ltp), 2)})
                self._save()
            self._write_snapshot(data, "trading session")
            _pause(POLL_SECONDS)

        # End of day: settle anything expiring today, then report.
        if self.last_bar and self.last_bar.date() == today:
            data.set_time(self.last_bar)
            for fill in settle_expiring(self.broker, data, self.last_bar):
                self.strategy.on_fill(ctx, fill)
            self._log_new_fills()
        if self.broker.positions():
            log.warning("Positions still open after close: %s",
                        {i.symbol: p.quantity for i, p in self.broker.positions().items()})
        day_fills = [f for f in self.broker.fills() if f.ts.date() == today]
        gross = sum((f.quantity * f.price) * (1 if f.side is Side.SELL else -1) for f in day_fills)
        charges = sum(f.charges for f in day_fills)
        row = {"date": today, "fills": len(day_fills), "gross": round(gross, 2), "charges": round(charges, 2),
               "net": round(gross - charges, 2), "equity": round(self.broker.equity(data.ltp), 2)}
        _append_csv(LOG_DIR / "daily.csv", row)
        log.info("=== %s closed: %s", today, row)
        self.day_closed = True
        self._save()
        self._write_snapshot(data, "session closed")

    def _write_snapshot(self, data: HistoricalMarketData | None, phase: str) -> None:
        """Small JSON for the dashboard: positions with last prices, pending stops, day status."""
        from data.indmoney_auth import CACHE_PATH, _read_cache

        def ltp(inst):
            if data is None or data._now is None:
                return None
            return data.ltp(inst)

        positions = []
        for inst, pos in self.broker.positions().items():
            last = ltp(inst)
            positions.append({"symbol": inst.symbol, "qty": pos.quantity, "avg_price": round(pos.avg_price, 2),
                              "ltp": last, "unrealized": round(pos.unrealized_pnl(last), 2) if last else None})
        orders = [{"symbol": o.instrument.symbol, "side": o.side.value, "type": o.order_type.value,
                   "qty": o.quantity, "trigger": o.trigger_price, "limit": o.limit_price, "tag": o.tag}
                  for o in self.broker.open_orders()]
        st = self.strategy
        today = getattr(st, "today", None)
        token = _read_cache(CACHE_PATH)
        snap = {
            "updated_at": ist_now().isoformat(timespec="seconds"),
            "phase": phase,
            "strategy": self.strategy_name,
            "day": str(self.day) if self.day else None,
            "last_bar": self.last_bar.isoformat() if self.last_bar else None,
            "underlying": today[0] if today else None,
            "spot": (data.spot(today[0]) if data is not None and data._now is not None and today else None),
            "vix_at_entry": getattr(st, "day_vix", None),
            "day_sl_pct": getattr(st, "day_sl_pct", None),
            "state": getattr(st, "state", None),
            "note": getattr(st, "note", None),
            "cash": round(self.broker.cash, 2),
            "equity": round(self.broker.equity(ltp if data is not None else (lambda i: None)), 2),
            "initial_capital": self.broker.initial_capital,
            "positions": positions,
            "open_orders": orders,
            "token_valid_until": token[1].isoformat(timespec="minutes") if token else None,
        }
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=2, default=str))
        tmp.replace(SNAPSHOT_PATH)

    def _log_new_fills(self) -> None:
        for f in self.broker.fills()[self._fills_logged:]:
            log.info("FILL %s %s %d @ %.2f (charges %.2f) [%s]", f.side.value, f.instrument.symbol,
                     f.quantity, f.price, f.charges, f.tag)
            _append_csv(LOG_DIR / "fills.csv", {
                "ts": f.ts, "symbol": f.instrument.symbol, "side": f.side.value, "qty": f.quantity,
                "price": round(f.price, 2), "charges": round(f.charges, 2), "tag": f.tag})
        self._fills_logged = len(self.broker.fills())

    def _setup_logfile(self, today: date) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        for h in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
            root.removeHandler(h)
        fh = logging.FileHandler(LOG_DIR / f"paper_{today:%Y%m%d}.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="exit after today's session")
    args = p.parse_args()

    load_env(HERE / ".env")
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    name = env("STRATEGY")
    if not name:
        raise SystemExit("Set STRATEGY in paper_trader/.env (see paper_trader/.env.example)")
    capital = float(env("CAPITAL")) if env("CAPITAL") else None
    lock = _single_instance_lock()  # noqa: F841 (held until exit)
    PaperBot(name, capital).run(once=args.once)


if __name__ == "__main__":
    main()
