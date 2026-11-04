"""Sanity-check the downloaded cache before trusting a backtest.

Usage: python -m data.audit [--from 2025-09-01] [--to 2026-11-03]

1. Index coverage   every session has full 09:15-15:30 candles for NIFTY, SENSEX
                    and India VIX (a missing index day makes the strategy skip it).
2. Option naming    every contract's trading_symbol matches its strike / type /
                    expiry, using the same option_symbol() the live feed uses, and
                    candles sit in the right exchange folder and end by expiry.
3. Expiry check     on expiry day the final CE/PE prices of near-the-money
                    strikes imply a spot (K + CE - PE) inside the index's
                    15:00-15:30 range (catches swapped CE/PE or another expiry's
                    prices; single-minute comparisons fail on fast closes).
4. Put-call parity  at 09:21 every session, ATM CE - PE ~= spot - strike for the
                    nearest expiry (catches wrong-expiry / wrong-underlying data).
"""
from __future__ import annotations

import argparse
from datetime import date, time

import pandas as pd

from data.store import DataStore
from data.underlyings import UNDERLYINGS, option_symbol

INTERVAL = "1minute"
FULL_SESSION = 375  # 09:15..15:29 (INDmoney also sends a 15:30 candle)
MAX_GAP_MIN = 5


def _index(store: DataStore, exchange: str, name: str, start: date, end: date) -> pd.DataFrame:
    return store.load_candles(INTERVAL, exchange, name).loc[str(start):str(end)]


def check_index_coverage(store, start, end) -> list[str]:
    frames = {n: _index(store, ex, n, start, end) for n, ex in
              (("NIFTY", "NSE"), ("SENSEX", "BSE"), ("INDIAVIX", "NSE"))}
    sessions = sorted(set().union(*(set(f.index.date) for f in frames.values())))
    problems = []
    for name, df in frames.items():
        by_day = {d: g for d, g in df.groupby(df.index.date)}
        for d in sessions:
            g = by_day.get(d)
            if g is None:
                problems.append(f"{d} {name}: NO candles (other indices traded that day)")
                continue
            gaps = g.index.to_series().diff().dt.total_seconds().div(60).max()
            if len(g) < FULL_SESSION - 5 or g.index[0].time() > time(9, 16) or (gaps or 0) > MAX_GAP_MIN:
                problems.append(f"{d} {name}: {len(g)} candles {g.index[0]:%H:%M}-{g.index[-1]:%H:%M}, "
                                f"largest gap {gaps:.0f} min")
    return [f"{len(sessions)} sessions checked"] + problems


def check_option_naming(store, start, end) -> list[str]:
    problems, contracts = [], 0
    for name, u in UNDERLYINGS.items():
        for expiry in store.expiries(name):
            if not (start <= expiry <= end):
                continue
            df = store.load_contracts(name, expiry)
            opts = df[df["option_type"].isin(["CE", "PE"])]
            contracts += len(opts)
            for r in opts.itertuples():
                expected = option_symbol(name, expiry, float(r.strike_price), r.option_type)
                if r.trading_symbol != expected or pd.to_datetime(r.expiry).date() != expiry:
                    problems.append(f"{name} {expiry}: {r.trading_symbol} != {expected} (expiry col {r.expiry})")
                if store.has_candles(INTERVAL, u.index_exchange.value, r.trading_symbol):
                    problems.append(f"{r.trading_symbol}: candles stored under {u.index_exchange.value}, "
                                    f"expected {u.option_exchange.value}")
                if store.has_candles(INTERVAL, u.option_exchange.value, r.trading_symbol):
                    c = store.load_candles(INTERVAL, u.option_exchange.value, r.trading_symbol)
                    if len(c) and c.index[-1].date() > expiry:
                        problems.append(f"{r.trading_symbol}: candles after expiry ({c.index[-1]})")
    return [f"{contracts} option contracts checked"] + problems[:50] + (
        [f"... {len(problems) - 50} more"] if len(problems) > 50 else [])


def _strike_frame(store, u, name, expiry, strike, opt_type):
    sym = option_symbol(name, expiry, strike, opt_type)
    return store.load_candles(INTERVAL, u.option_exchange.value, sym)


def check_expiry_settlement(store, start, end) -> list[str]:
    """On expiry day the last CE/PE candles of a near-the-money strike imply a spot (K + CE - PE) that the
    index actually traded at in the closing half hour. Wrong expiry / swapped CE-PE breaks this badly."""
    problems, checked = [], 0
    for name, u in UNDERLYINGS.items():
        idx = store.load_candles(INTERVAL, u.index_exchange.value, name)
        for expiry in store.expiries(name):
            if not (start <= expiry <= end):
                continue
            closing = idx.loc[str(expiry):str(expiry)].between_time("15:00", "15:30")
            if closing.empty:
                problems.append(f"{name} {expiry}: no index candles on expiry day, can't check")
                continue
            lo, hi = float(closing["low"].min()), float(closing["high"].max())
            tol = 0.002 * hi
            k0 = round(float(closing["close"].iloc[-1]) / u.strike_step) * u.strike_step
            for k in (k0 - 2 * u.strike_step, k0, k0 + 2 * u.strike_step):
                ce = _strike_frame(store, u, name, expiry, k, "CE").loc[str(expiry):str(expiry)]
                pe = _strike_frame(store, u, name, expiry, k, "PE").loc[str(expiry):str(expiry)]
                both = ce.index.intersection(pe.index)
                if both.empty:
                    continue
                checked += 1
                ts = both[-1]
                implied = k + float(ce.loc[ts, "close"]) - float(pe.loc[ts, "close"])
                if not (lo - tol <= implied <= hi + tol):
                    problems.append(f"{name} {expiry} K={k:g}: implied spot {implied:.2f} at {ts:%H:%M} outside "
                                    f"index range {lo:.2f}-{hi:.2f} (15:00-15:30)")
    return [f"{checked} near-the-money strikes checked on expiry days"] + problems


def check_put_call_parity(store, start, end) -> list[str]:
    problems, checked, worst = [], 0, 0.0
    for name, u in UNDERLYINGS.items():
        idx = _index(store, u.index_exchange.value, name, start, end)
        expiries = store.expiries(name)
        for d, g in idx.groupby(idx.index.date):
            at = g.between_time("09:21", "09:21")
            nxt = [e for e in expiries if e >= d]
            if at.empty or not nxt:
                continue
            spot, expiry = float(at["close"].iloc[0]), nxt[0]
            k = round(spot / u.strike_step) * u.strike_step
            ce, pe = (_strike_frame(store, u, name, expiry, k, t) for t in ("CE", "PE"))
            ts = at.index[0]
            if ts not in ce.index or ts not in pe.index:
                continue  # strike not downloaded for this day (e.g. far from that week's range)
            checked += 1
            dev = (float(ce.loc[ts, "close"]) - float(pe.loc[ts, "close"])) - (spot - k)
            worst = max(worst, abs(dev) / spot)
            if abs(dev) > 0.004 * spot:  # forward carry for <= 1 week is ~0.1-0.2% of spot
                problems.append(f"{d} {name} {expiry} K={k:g}: CE-PE minus (S-K) = {dev:+.2f} "
                                f"({dev / spot:+.2%} of spot)")
    return [f"{checked} sessions checked, worst deviation {worst:.2%} of spot"] + problems


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2025, 9, 1))
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=date.today())
    args = p.parse_args()
    store = DataStore()
    failed = False
    for title, check in (("1. Index coverage", check_index_coverage),
                         ("2. Option naming", check_option_naming),
                         ("3. Expiry check", check_expiry_settlement),
                         ("4. Put-call parity", check_put_call_parity)):
        lines = check(store, args.start, args.end)
        print(f"\n{title}: {lines[0]}" + ("" if len(lines) > 1 else " -- OK"))
        for line in lines[1:]:
            print("   ", line)
        failed |= len(lines) > 1
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
