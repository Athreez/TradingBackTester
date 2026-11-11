"""Replay past sessions through the paper bot's engine and write its journals, to view on the dashboard.

Usage: python -m paper_trader.simulate --from 2026-10-01 --to 2026-11-03 [--strategy vix_iron_fly] [--capital 500000]
       python -m paper_trader.simulate --from 2026-10-01 --to 2026-11-03 --into-paper-account

Uses the downloaded candle cache (data/cache) and the same per-minute step
(backtester.engine.run_bar), end-of-day settlement and journal format as the
live paper bot.

Default: output goes to paper_trader/sim/{logs,state} (wiped first) and is
shown by the dashboard's "Simulation" switch; the paper account is untouched.

--into-paper-account: the simulated sessions BECOME the paper account's
history. The current paper_trader/logs and state are moved to
paper_trader/backup/<timestamp>/, the bot's state file is written, and the
live bot continues from the simulated account the next time it starts. The
bot must not be running.
"""
from __future__ import annotations

import argparse
import logging
import shutil
from datetime import date, datetime
from itertools import groupby
from pathlib import Path

from backtester.engine import run_bar
from backtester.historical_data import HistoricalMarketData
from backtester.sim_broker import SimBroker
from core.config import env, load_env
from core.costs import CostModel
from core.logsafe import protect_handlers
from core.registry import load_strategy
from core.strategy_base import Context
from data.store import DataStore
from paper_trader import runner
from paper_trader.runner import (BAR, HERE, PREPARE_AT, CLOSE_AT, _append_csv, close_day, log_fills,
                                 save_state, setup_day_logfile, write_snapshot)

SIM_DIR = HERE / "sim"
SIMULATED_MARKER = "simulated_history.json"  # in state/: tells the dashboard which days were simulated
log = logging.getLogger("paper")


class SimClockFormatter(logging.Formatter):
    """Stamps log lines with the simulated market time instead of the wall clock."""

    def __init__(self, clock: dict):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.clock = clock

    def formatTime(self, record, datefmt=None):
        now = self.clock.get("now")
        return now.strftime("%Y-%m-%d %H:%M:%S,000") if now else super().formatTime(record, datefmt)


def simulate(strategy_name: str, start: date, end: date, capital: float | None = None,
             out: Path = SIM_DIR, store: DataStore | None = None, overrides: dict | None = None,
             wipe: bool = True, write_state: bool = False) -> dict:
    if wipe:
        for sub in ("logs", "state"):
            shutil.rmtree(out / sub, ignore_errors=True)
    log_dir = out / "logs"

    strategy, config = load_strategy(strategy_name, overrides)
    interval = config.get("interval", "1minute")
    capital = capital or float(config.get("capital", 500_000))
    data_start = config.get("data_start")
    if data_start and start < data_start:
        start = data_start
    excluded = set(config.get("exclude_dates") or [])

    data = HistoricalMarketData(store or DataStore(), interval)
    stamps = set()
    for u in config["underlyings"]:
        stamps.update(data.index_frame(u).loc[str(start):str(end)].index)
    timeline = [ts.to_pydatetime() for ts in sorted(stamps) if ts.date() not in excluded]
    if not timeline:
        raise SystemExit(f"No index candles between {start} and {end}; download them with data.downloader first")

    broker = SimBroker(lambda inst: data.bar_at(inst, data.now()), CostModel.from_dict(config.get("costs")), capital)
    ctx = Context(data, broker)
    clock: dict = {"now": None}
    fmt = SimClockFormatter(clock)
    root = logging.getLogger()
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)  # the per-day log files record the session at INFO detail
    logged, last_bar, days = 0, None, []

    for day, bars in groupby(timeline, key=lambda t: t.date()):
        setup_day_logfile(log_dir, day, fmt)
        clock["now"] = datetime.combine(day, PREPARE_AT)
        log.info("=== %s: preparing session (simulation)", day)
        for ts in bars:
            clock["now"] = ts + BAR
            run_bar(strategy, ctx, broker, data, ts)
            last_bar = ts
            log_fills(broker.fills()[logged:], log_dir)
            logged = len(broker.fills())
            _append_csv(log_dir / "equity.csv", {"ts": ts, "equity": round(broker.equity(data.ltp), 2)})
        clock["now"] = datetime.combine(day, CLOSE_AT)
        days.append(close_day(strategy, ctx, broker, data, day, last_bar, log_dir))
        log_fills(broker.fills()[logged:], log_dir)
        logged = len(broker.fills())

    last_day = days[-1]["date"]
    if write_state:  # becomes the paper account: the live bot resumes from here
        save_state(out / "state" / "state.pkl", strategy_name, broker, strategy, last_bar, last_day, True)
        (out / "state" / SIMULATED_MARKER).write_text(
            f'{{"from": "{start}", "to": "{last_day}", "created": "{datetime.now():%Y-%m-%d %H:%M}"}}')
        write_snapshot(out / "state" / "live.json", phase=f"history {start} to {last_day} simulated; "
                       "live paper trading continues from the next session",
                       strategy_name=strategy_name, broker=broker, strategy=strategy, data=data,
                       day=last_day, last_bar=last_bar)
    else:
        write_snapshot(out / "state" / "live.json", phase=f"simulation {start} to {end} finished",
                       strategy_name=strategy_name, broker=broker, strategy=strategy, data=data,
                       day=last_day, last_bar=last_bar,
                       extra={"simulation": {"from": str(start), "to": str(end)}})
    for h in [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]:
        logging.getLogger().removeHandler(h)
        h.close()
    traded = [d for d in days if d["fills"]]
    return {"days": len(days), "traded": len(traded), "won": sum(d["net"] > 0 for d in traded),
            "net": round(sum(d["net"] for d in days), 2), "charges": round(sum(d["charges"] for d in days), 2),
            "equity": days[-1]["equity"]}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    p.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    p.add_argument("--strategy", help="default: STRATEGY from .env")
    p.add_argument("--capital", type=float, help="default: CAPITAL from .env")
    p.add_argument("--into-paper-account", action="store_true",
                   help="make the simulated sessions the paper account's history (old files are backed up)")
    args = p.parse_args()

    load_env()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    protect_handlers()
    logging.getLogger().handlers[0].setLevel(logging.WARNING)  # console: problems only; full detail in the logs
    name = args.strategy or env("STRATEGY") or "vix_iron_fly"
    capital = args.capital or (float(env("CAPITAL")) if env("CAPITAL") else None)
    if not args.into_paper_account:
        result = simulate(name, args.start, args.end, capital)
        _report(name, args, result)
        print(f"Written to {SIM_DIR}. Open the dashboard and pick 'Simulation' in the sidebar.")
        return

    lock = runner._single_instance_lock()  # noqa: F841 -- exits if the bot is running; blocks it from starting
    backup = HERE / "backup" / datetime.now().strftime("%Y%m%d_%H%M%S")
    moved = []
    for path in [HERE / "logs", *(HERE / "state").glob("*")]:
        if path.exists() and path.name != "bot.lock":
            target = backup / path.relative_to(HERE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            moved.append(str(path.relative_to(HERE)))
    if moved:
        print(f"Backed up {', '.join(moved)} to {backup}")
    result = simulate(name, args.start, args.end, capital, out=HERE, wipe=False, write_state=True)
    _report(name, args, result)
    print("This is now the paper account's history. Start the bot (start_paper_trading.bat) to continue live.")


def _report(name: str, args, result: dict) -> None:
    print(f"Simulated {name} {args.start} -> {args.end}: {result['days']} sessions, {result['traded']} traded "
          f"({result['won']} won), net {result['net']:+,.2f} after {result['charges']:,.2f} charges, "
          f"equity {result['equity']:,.2f}")


if __name__ == "__main__":
    main()
