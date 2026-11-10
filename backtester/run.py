"""Backtest any strategy in strategies/.

Usage:
  python -m backtester.run --list
  python -m backtester.run --strategy vix_iron_fly --from 2025-09-01 --to 2026-10-30
  python -m backtester.run --strategy vix_iron_fly --from 2025-09-01 --to 2026-10-30 \
      --set schedule.NIFTY.lots=5 --set schedule.SENSEX.lots=5 --label 5lots
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path

import yaml

from backtester.engine import BacktestEngine
from backtester.historical_data import HistoricalMarketData
from backtester.metrics import fills_frame, summarize
from backtester.sim_broker import SimBroker
from core.config import ROOT
from core.costs import CostModel
from core.registry import available_strategies, load_strategy
from data.store import DataStore


def run_backtest(strategy_name: str, start: date, end: date, store: DataStore | None = None,
                 capital: float | None = None, overrides: dict | None = None):
    strategy, config = load_strategy(strategy_name, overrides)
    interval = config.get("interval", "1minute")
    underlyings = config["underlyings"]
    capital = capital or float(config.get("capital", 500_000))
    data_start = config.get("data_start") if store is None else None  # data_start describes data/cache
    if data_start and start < data_start:
        print(f"Note: {strategy_name} only uses data from {data_start}; ignoring {start} -> {data_start}")
        start = data_start

    data = HistoricalMarketData(store or DataStore(), interval)
    excluded = set(config.get("exclude_dates") or [])
    stamps = set()
    for u in underlyings:
        stamps.update(data.index_frame(u).loc[str(start):str(end)].index)
    timeline = [ts.to_pydatetime() for ts in sorted(stamps) if ts.date() not in excluded]

    broker = SimBroker(
        quote=lambda inst: data.bar_at(inst, data.now()),
        costs=CostModel.from_dict(config.get("costs")),
        initial_capital=capital,
    )
    result = BacktestEngine(strategy, data, broker, timeline).run()
    result.meta = {"strategy": strategy_name, "underlyings": underlyings, "interval": interval,
                   "params": strategy.params}
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="list available strategies")
    p.add_argument("--strategy")
    p.add_argument("--from", dest="start", type=date.fromisoformat)
    p.add_argument("--to", dest="end", type=date.fromisoformat)
    p.add_argument("--capital", type=float)
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="override a strategy param, dotted path into config.yaml params (repeatable)")
    p.add_argument("--label", default="", help="suffix for the report folder name")
    p.add_argument("--data-root", type=Path, help="DataStore folder to use instead of data/cache "
                   "(e.g. data/cache/hf_store from data.import_hf)")
    args = p.parse_args()

    if args.list:
        print("\n".join(available_strategies()))
        return
    if not (args.strategy and args.start and args.end):
        p.error("--strategy, --from and --to are required")

    overrides = {}
    for item in args.overrides:
        key, sep, value = item.partition("=")
        if not sep:
            p.error(f"--set expects KEY=VALUE, got '{item}'")
        overrides[key.strip()] = yaml.safe_load(value)

    store = DataStore(args.data_root) if args.data_root else None
    result = run_backtest(args.strategy, args.start, args.end, store=store, capital=args.capital, overrides=overrides)
    result.meta["overrides"] = overrides
    result.meta["data_root"] = str(args.data_root or "data/cache")
    summary = summarize(result)

    label = "_" + re.sub(r"[^A-Za-z0-9_-]", "", args.label) if args.label else ""
    out = ROOT / "reports" / f"{args.strategy}_{datetime.now():%Y%m%d_%H%M%S}{label}"
    out.mkdir(parents=True)
    (out / "summary.json").write_text(json.dumps({**result.meta, **summary}, indent=2, default=str))
    result.equity.to_csv(out / "equity.csv")
    fills_frame(result).to_csv(out / "fills.csv", index=False)

    width = max(map(len, summary))
    for k, v in summary.items():
        print(f"{k:<{width}}  {v}")
    print(f"\nReport saved to {out}")


if __name__ == "__main__":
    main()
