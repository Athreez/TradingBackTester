"""Replay past sessions through the paper bot's engine and write its journals, to view on the dashboard.

Usage: python -m paper_trader.simulate --from 2026-10-01 --to 2026-11-03 [--strategy vix_iron_fly] [--capital 500000]

Uses the downloaded candle cache (data/cache) and the same per-minute step
(backtester.engine.run_bar), end-of-day settlement and journal format as the
live paper bot. Output goes to paper_trader/sim/{logs,state} (wiped first);
the real paper account in paper_trader/{logs,state} is never touched.
View it with the dashboard's "Simulation" switch.
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
from core.registry import load_strategy
from core.strategy_base import Context
from data.store import DataStore
from paper_trader.runner import (BAR, HERE, PREPARE_AT, CLOSE_AT, _append_csv, close_day, log_fills,
                                 setup_day_logfile, write_snapshot)

SIM_DIR = HERE / "sim"
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
             out: Path = SIM_DIR, store: DataStore | None = None, overrides: dict | None = None) -> dict:
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

    write_snapshot(out / "state" / "live.json", phase=f"simulation {start} to {end} finished",
                   strategy_name=strategy_name, broker=broker, strategy=strategy, data=data,
                   day=days[-1]["date"], last_bar=last_bar,
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
    p.add_argument("--strategy", help="default: STRATEGY from paper_trader/.env")
    p.add_argument("--capital", type=float, help="default: CAPITAL from paper_trader/.env")
    args = p.parse_args()

    load_env(HERE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger().handlers[0].setLevel(logging.WARNING)  # console: problems only; full detail in the logs
    name = args.strategy or env("STRATEGY") or "vix_iron_fly"
    capital = args.capital or (float(env("CAPITAL")) if env("CAPITAL") else None)
    result = simulate(name, args.start, args.end, capital)
    print(f"Simulated {name} {args.start} -> {args.end}: {result['days']} sessions, {result['traded']} traded "
          f"({result['won']} won), net {result['net']:+,.2f} after {result['charges']:,.2f} charges, "
          f"equity {result['equity']:,.2f}")
    print(f"Written to {SIM_DIR}. Open the dashboard and pick 'Simulation' in the sidebar.")


if __name__ == "__main__":
    main()
