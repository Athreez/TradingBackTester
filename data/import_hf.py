"""Convert the Hugging Face dataset codepyx23/india-index-options-1m into a DataStore for backtests.

Usage:
  python -m data.import_hf [--src data/cache/hf] [--dest data/cache/hf_store] [--to 2026-07-31]

--src holds the dataset as downloaded (index/<U>.parquet, options/<U>/<expiry>.parquet). The result is
a separate store (never mixed into data/cache), with the same layout and option symbols as INDmoney's.

India VIX is not in the dataset. Its 1-minute candles come from the INDmoney cache (available from
21-May-2024); before that each day gets flat bars at that day's INDmoney daily VIX open, a stand-in
that picks the same VIX regime as 09:22 on ~91% of the days where both exist. The stand-in days are
listed in <dest>/vix_standin_days.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
from datetime import date, time
from pathlib import Path

import pandas as pd

from data.store import CACHE_DIR, DataStore
from data.underlyings import get_underlying, option_symbol

log = logging.getLogger("import_hf")
SESSION = (time(9, 15), time(15, 30))


def _bars(df: pd.DataFrame) -> pd.DataFrame:
    """Dataset rows -> our candle frame: tz-naive IST `ts` index, regular session only, no duplicates."""
    ts = df["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    out = df.assign(ts=ts).drop_duplicates("ts", keep="last").set_index("ts").sort_index()
    t = out.index.time
    out = out[(t >= SESSION[0]) & (t <= SESSION[1])]
    return out[["open", "high", "low", "close", "volume"]].astype({"volume": "int64"})


def import_underlying(src: Path, store: DataStore, underlying: str, end: date) -> list[date]:
    u = get_underlying(underlying)
    index = _bars(pd.read_parquet(src / "index" / f"{underlying}.parquet"))
    index = index[index.index.date <= end]
    store.save_candles("1minute", u.index_exchange.value, underlying, index)
    log.info("%s index: %d candles %s -> %s", underlying, len(index), index.index[0], index.index[-1])

    for path in sorted((src / "options" / underlying).glob("*.parquet")):
        expiry = date.fromisoformat(path.stem)
        if expiry > end:
            continue
        try:
            u.lot_size(expiry)
        except ValueError:
            continue  # before the lot sizes we model (NIFTY weeklies before Aug 2021)
        df = pd.read_parquet(path)
        rows = []
        for (strike, opt), g in df.groupby(["strike", "option_type"]):
            sym = option_symbol(underlying, expiry, float(strike), opt)
            store.save_candles("1minute", u.option_exchange.value, sym, _bars(g))
            rows.append({"trading_symbol": sym, "strike_price": float(strike), "option_type": opt,
                         "expiry": expiry})
        store.save_contracts(underlying, expiry, pd.DataFrame(rows))
        log.info("%s %s: %d option series", underlying, expiry, len(rows))
    return sorted(set(index.index.date))


def import_vix(store: DataStore, days: list[date]) -> list[date]:
    src = DataStore(CACHE_DIR)
    minute = src.load_candles("1minute", "NSE", "INDIAVIX")
    daily = src.load_candles("1day", "NSE", "INDIAVIX")
    daily_open = daily.groupby(daily.index.date)["open"].first()
    real_days = set(minute.index.date)
    nifty = store.load_candles("1minute", "NSE", "NIFTY")
    stamps = nifty.index.union(store.load_candles("1minute", "BSE", "SENSEX").index)

    standin, frames = [], [minute[pd.Index(minute.index.date).isin(set(days))]]
    for day in days:
        if day in real_days or day not in daily_open.index:
            continue
        idx = stamps[stamps.date == day]
        v = float(daily_open[day])
        frames.append(pd.DataFrame({"open": v, "high": v, "low": v, "close": v, "volume": 0},
                                   index=pd.DatetimeIndex(idx, name="ts")))
        standin.append(day)
    store.save_candles("1minute", "NSE", "INDIAVIX", pd.concat(frames).sort_index())
    missing = [d for d in days if d not in real_days and d not in daily_open.index]
    if missing:
        log.warning("No India VIX at all for %d days (they will not trade): %s", len(missing), missing[:10])
    return standin


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", type=Path, default=CACHE_DIR / "hf")
    p.add_argument("--dest", type=Path, default=CACHE_DIR / "hf_store")
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=date.max)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    shutil.rmtree(args.dest, ignore_errors=True)  # rebuilt from scratch each time
    store = DataStore(args.dest)
    days = sorted(set(import_underlying(args.src, store, "NIFTY", args.end))
                  | set(import_underlying(args.src, store, "SENSEX", args.end)))
    standin = import_vix(store, days)
    (args.dest / "vix_standin_days.json").write_text(json.dumps([d.isoformat() for d in standin]))
    log.info("India VIX: %d days from 1-minute data, %d days on the daily stand-in", len(days) - len(standin),
             len(standin))


if __name__ == "__main__":
    main()
