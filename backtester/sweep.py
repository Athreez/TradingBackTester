"""Run one strategy across a range of values for a single param, in parallel.

Usage:
  python -m backtester.sweep --strategy iron_fly --from 2025-09-01 --to 2026-10-30 \
      --param stop_loss_pct=30,40,50,60 [--set skip_expiry_day=false] [--label name]

Prints a comparison table and saves it to reports/sweep_<strategy>_<timestamp>_<label>.csv.
"""
from __future__ import annotations

import argparse
import os
import re
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime

import pandas as pd
import yaml

from backtester.metrics import summarize
from backtester.run import run_backtest
from core.config import ROOT
from core.models import Side


def daily_net(result) -> pd.Series:
    """Net P&L per traded day (premium cash flow minus charges)."""
    rows = [(f.ts.date(), (f.quantity * f.price if f.side is Side.SELL else -f.quantity * f.price) - f.charges)
            for f in result.fills]
    return pd.DataFrame(rows, columns=["day", "net"]).groupby("day").net.sum()


def _run_one(job: tuple) -> dict:
    strategy, start, end, overrides, key, value = job
    result = run_backtest(strategy, start, end, overrides={**overrides, key: value})
    s = summarize(result)
    daily = daily_net(result)
    by_dow = daily.groupby(pd.to_datetime(daily.index).day_name().str[:3]).sum()
    monthly = daily.groupby(pd.to_datetime(daily.index).to_period("M")).sum()
    return {
        key: value,
        "net_pnl": s["net_pnl"],
        "charges": s["total_charges"],
        "max_dd": s["max_drawdown"],
        "days": len(daily),
        "win_pct": round((daily > 0).mean() * 100, 1),
        "worst_day": round(daily.min(), 2),
        "months_up": f"{(monthly > 0).sum()}/{len(monthly)}",
        **{dow: round(by_dow.get(dow, 0.0)) for dow in ("Mon", "Tue", "Wed", "Thu", "Fri") if dow in by_dow},
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy", required=True)
    p.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    p.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    p.add_argument("--param", required=True, metavar="KEY=V1,V2,...")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--label", default="")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = p.parse_args()

    key, _, values = args.param.partition("=")
    overrides = {}
    for item in args.overrides:
        k, _, v = item.partition("=")
        overrides[k.strip()] = yaml.safe_load(v)
    jobs = [(args.strategy, args.start, args.end, overrides, key, yaml.safe_load(v)) for v in values.split(",")]

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        table = pd.DataFrame(list(pool.map(_run_one, jobs)))

    pd.set_option("display.width", 200)
    print(table.to_string(index=False))
    label = "_" + re.sub(r"[^A-Za-z0-9_-]", "", args.label) if args.label else ""
    out = ROOT / "reports" / f"sweep_{args.strategy}_{datetime.now():%Y%m%d_%H%M%S}{label}.csv"
    out.parent.mkdir(exist_ok=True)
    table.to_csv(out, index=False)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
