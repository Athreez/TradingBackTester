"""Pull historical data from INDmoney into the local cache.

Usage:
  python -m data.downloader token [--new]   # show token status / force a new TOTP token
  python -m data.downloader check
  python -m data.downloader index   --underlying NIFTY|SENSEX|INDIAVIX --from 2025-01-01 --to 2025-03-31 --interval 1minute
  python -m data.downloader options --underlying NIFTY --from 2025-01-01 --to 2025-03-31 --interval 1minute

`options` downloads, for every expired weekly expiry in the range, only the
strikes the index actually traded near during that contract's last
`--days-before` days (+/- `--strike-buffer`), which keeps API calls small.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import requests

from core.config import load_env
from data.indmoney_auth import generate_token, token_status, totp_configured
from data.indmoney_client import MAX_LIVE_SCRIPS, IndMoneyClient, IndMoneyError
from data.store import CACHE_DIR, DataStore
from data.underlyings import SIGNAL_INDICES, UNDERLYINGS, get_underlying, index_spec, option_symbol

log = logging.getLogger("downloader")

# The contracts endpoints document `segment=DERIVATIVE`; candles use NFO/BFO.
CONTRACTS_SEGMENT = "DERIVATIVE"


def _client() -> IndMoneyClient:
    load_env()
    return IndMoneyClient.from_env()


def _index_code(client: IndMoneyClient, store: DataStore, underlying: str) -> str:
    code = store.get_index_code(underlying)
    if code is None:
        index_name, exchange = index_spec(underlying)
        code = client.index_scrip_code(index_name, exchange.value)
        store.set_index_code(underlying, code)
        log.info("Resolved %s index scrip code: %s", underlying, code)
    return code


def download_index(client, store, underlying: str, start: date, end: date, interval: str) -> None:
    _, exchange = index_spec(underlying)
    code = _index_code(client, store, underlying)
    frames = client.historical([code], interval, datetime.combine(start, time.min),
                               datetime.combine(end + timedelta(days=1), time.min))
    df = frames.get(code)
    if df is None or df.empty:
        log.warning("No index candles returned for %s (%s)", underlying, code)
        return
    store.save_candles(interval, exchange.value, underlying, df)
    log.info("%s: saved %d index candles (%s -> %s)", underlying, len(df), df.index[0], df.index[-1])


def download_options(client, store, underlying: str, start: date, end: date, interval: str,
                     days_before: int, strike_buffer: float) -> list[date]:
    """Expired contracts in [start, end]; returns their expiries."""
    u = get_underlying(underlying)
    download_index(client, store, underlying, start - timedelta(days=days_before), end, interval)
    index_df = store.load_candles(interval, u.index_exchange.value, underlying)

    expiries = client.expired_expiries(underlying, CONTRACTS_SEGMENT, start, end)
    log.info("%s: %d expired expiries between %s and %s", underlying, len(expiries), start, end)

    for expiry in expiries:
        contracts = client.expired_contracts(underlying, CONTRACTS_SEGMENT, expiry)
        if contracts.empty:
            log.warning("%s %s: no contracts returned", underlying, expiry)
            continue
        store.save_contracts(underlying, expiry, contracts)

        w_start = datetime.combine(expiry - timedelta(days=days_before), time.min)
        w_end = datetime.combine(expiry + timedelta(days=1), time.min)
        spot = index_df.loc[w_start:w_end]
        if spot.empty:
            log.warning("%s %s: no index data in window, skipping options", underlying, expiry)
            continue
        lo, hi = spot["low"].min() * (1 - strike_buffer), spot["high"].max() * (1 + strike_buffer)

        opts = contracts[contracts["option_type"].isin(["CE", "PE"])]
        opts = opts[(opts["strike_price"] >= lo) & (opts["strike_price"] <= hi)]
        last_day = min(expiry, end)
        symbols = [s for s in opts["trading_symbol"]
                   if not _covers(store, interval, u.option_exchange.value, s, last_day)]
        if not symbols:
            continue
        frames = client.expired_historical(symbols, interval, w_start, w_end, u.option_exchange.value)
        for sym, df in frames.items():
            store.save_candles(interval, u.option_exchange.value, sym, df)
        log.info("%s %s: saved %d/%d option series (strikes %.0f-%.0f)",
                 underlying, expiry, len(frames), len(symbols), lo, hi)
    return expiries


def _covers(store, interval: str, exchange: str, symbol: str, last_day: date) -> bool:
    """True if the cached series already reaches last_day. A contract first fetched while it was still
    live (download_live_options) stops at that day and must be completed once it has expired."""
    if not store.has_candles(interval, exchange, symbol):
        return False
    df = store.load_candles(interval, exchange, symbol)
    return len(df) > 0 and df.index[-1].date() >= last_day


def _historical_skipping_invalid(client, codes: list[str], interval: str, start: datetime, end: datetime) -> dict:
    """One unknown code makes INDmoney reject its whole batch of 5 ("Invalid scrip codes"); retry that
    batch code by code and skip the bad ones."""
    out = {}
    for i in range(0, len(codes), MAX_LIVE_SCRIPS):
        batch = codes[i:i + MAX_LIVE_SCRIPS]
        try:
            out.update(client.historical(batch, interval, start, end))
        except IndMoneyError as e:
            if "Invalid scrip codes" not in str(e):
                raise
            for code in batch:
                try:
                    out.update(client.historical([code], interval, start, end))
                except IndMoneyError as e2:
                    if "Invalid scrip codes" not in str(e2):
                        raise
                    log.warning("skipping %s: INDmoney does not recognise it for historical data", code)
    return out


def download_live_options(client, store, underlying: str, start: date, end: date, interval: str,
                          days_before: int, strike_buffer: float, already_expired: set[date] = frozenset()) -> None:
    """Contracts that have NOT expired yet (e.g. this week's) are not in the expired endpoints; take them
    from the live instrument master and fetch their candles by scrip code, saved under the same
    symbols the expired data uses, so a later expired download merges into them."""
    u = get_underlying(underlying)
    index_df = store.load_candles(interval, u.index_exchange.value, underlying)
    master = client.instruments("fno")
    rows = master[master["TRADING_SYMBOL"].astype(str).str.startswith(f"{underlying}-")
                  & master["OPTION_TYPE"].isin(["CE", "PE"])].copy()
    rows["expiry"] = pd.to_datetime(rows["EXPIRY_DATE"], format="%m/%d/%Y %H:%M").dt.date
    # Only contracts still live today: anything earlier is (or will be) in the expired endpoints.
    # A contract expiring today is still listed live but is already served by the expired endpoints.
    expiries = sorted(e for e in rows["expiry"].unique()
                      if e >= max(start, date.today()) and e - timedelta(days=days_before) <= end
                      and e not in already_expired)
    for expiry in expiries:
        w_start = datetime.combine(expiry - timedelta(days=days_before), time.min)
        w_end = datetime.combine(min(expiry, end) + timedelta(days=1), time.min)
        spot = index_df.loc[w_start:w_end]
        if spot.empty:
            log.warning("%s %s (live): no index data in window, skipping", underlying, expiry)
            continue
        lo, hi = spot["low"].min() * (1 - strike_buffer), spot["high"].max() * (1 + strike_buffer)
        chain = rows[rows["expiry"] == expiry]
        strikes = chain["STRIKE_PRICE"].astype(float)
        symbols = [option_symbol(underlying, expiry, k, t) for k, t in zip(strikes, chain["OPTION_TYPE"])]
        store.save_contracts(underlying, expiry, pd.DataFrame({
            "trading_symbol": symbols, "strike_price": strikes.values,
            "option_type": chain["OPTION_TYPE"].values, "expiry": expiry}))
        wanted = {f"{u.option_exchange.value}_{int(sid)}": sym
                  for sym, k, sid in zip(symbols, strikes, chain["SECURITY_ID"]) if lo <= k <= hi}
        frames = _historical_skipping_invalid(client, list(wanted), interval, w_start, w_end)
        for code, df in frames.items():
            store.save_candles(interval, u.option_exchange.value, wanted[code], df)
        log.info("%s %s (not expired yet): saved %d/%d option series up to %s (strikes %.0f-%.0f)",
                 underlying, expiry, len(frames), len(wanted), min(expiry, end), lo, hi)


YAHOO_SYMBOLS = {"NIFTY": "^NSEI", "SENSEX": "^BSESN"}
GAP_CHECK_INDICES = (("NIFTY", "NSE"), ("SENSEX", "BSE"), ("INDIAVIX", "NSE"))
MAX_SOURCE_DIFF = 0.0005  # Yahoo vs INDmoney mean |close diff| must be under 0.05% of the level


def _yahoo_day(symbol: str, day: date) -> pd.DataFrame:
    """One session of 1-minute candles from Yahoo Finance (kept for roughly the last 7 days)."""
    ist = timezone(timedelta(hours=5, minutes=30))
    a = int(datetime.combine(day, time(9, 0), ist).timestamp())
    b = int(datetime.combine(day, time(16, 0), ist).timestamp())
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"interval": "1m", "period1": a, "period2": b},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = (r.json().get("chart", {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        return pd.DataFrame()
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close")},
                      index=pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None))
    df.index.name = "ts"
    df = df.dropna().round(2).assign(volume=0.0)
    return df[df.index.date == day]


def fill_index_gaps(store, underlying: str, start: date, end: date, interval: str) -> None:
    """Fill sessions where INDmoney has no candles for this index (other indices traded) from Yahoo,
    after checking Yahoo against INDmoney on the most recent day both have."""
    exchange = index_spec(underlying)[1].value
    frames = {n: store.load_candles(interval, ex, n).loc[str(start):str(end)] for n, ex in GAP_CHECK_INDICES}
    sessions = sorted(set().union(*(set(f.index.date) for f in frames.values())))
    have = set(frames[underlying].index.date)
    gaps = [d for d in sessions if d not in have]
    if not gaps:
        log.info("%s: no gaps between %s and %s", underlying, start, end)
        return
    symbol = YAHOO_SYMBOLS[underlying]
    ours = store.load_candles(interval, exchange, underlying)
    reference = None
    for d in sorted(have, reverse=True)[:5]:  # latest day both sources have
        y = _yahoo_day(symbol, d)
        if y.empty:
            continue
        both = ours.loc[str(d):str(d)][["close"]].join(y[["close"]].rename(columns={"close": "yahoo"}), how="inner")
        if len(both) >= 300:  # enough overlapping minutes to judge the source
            diff = (both["close"] - both["yahoo"]).abs()
            reference = (d, float(diff.mean()), float(diff.max()), float(both["close"].mean()))
            break
    if reference is None:
        raise SystemExit(f"Yahoo has no recent {symbol} day overlapping INDmoney to validate against; not filling")
    ref_day, mean_diff, max_diff, level = reference
    if not mean_diff <= MAX_SOURCE_DIFF * level:  # written so NaN fails too
        raise SystemExit(f"Yahoo {symbol} differs from INDmoney on {ref_day} by {mean_diff:.2f} on average; not filling")
    log.info("%s: Yahoo matches INDmoney on %s (mean |close diff| %.2f, max %.2f)", underlying, ref_day, mean_diff, max_diff)

    patches_path = CACHE_DIR / "external_patches.json"
    patches = json.loads(patches_path.read_text()) if patches_path.exists() else []
    for d in gaps:
        y = _yahoo_day(symbol, d)
        if len(y) < 300:
            log.warning("%s %s: Yahoo has only %d candles (too old or incomplete); left as a gap", underlying, d, len(y))
            continue
        store.save_candles(interval, exchange, underlying, y[["open", "high", "low", "close", "volume"]])
        patches.append({"series": f"{exchange}/{underlying} {interval}", "date": str(d), "source": f"Yahoo Finance {symbol} 1m",
                        "reason": "INDmoney returned no candles for this session",
                        "validated": f"{ref_day} vs INDmoney: close mean |diff| {mean_diff:.2f}, max {max_diff:.2f}",
                        "added": datetime.now().isoformat(timespec="minutes")})
        log.info("%s %s: filled %d candles from Yahoo %s", underlying, d, len(y), symbol)
    patches_path.write_text(json.dumps(patches, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("token", help="show token status; --new generates a fresh TOTP token")
    t.add_argument("--new", action="store_true")
    sub.add_parser("check", help="verify the access token works")
    g = sub.add_parser("fill-gaps", help="fill sessions INDmoney has no index candles for, from Yahoo (validated)")
    g.add_argument("--underlying", required=True, choices=list(YAHOO_SYMBOLS))
    g.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    g.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    g.add_argument("--interval", default="1minute")
    for name in ("index", "options"):
        s = sub.add_parser(name)
        choices = list(UNDERLYINGS) + (list(SIGNAL_INDICES) if name == "index" else [])
        s.add_argument("--underlying", required=True, choices=choices)
        s.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
        s.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
        s.add_argument("--interval", default="1minute")
        if name == "options":
            s.add_argument("--days-before", type=int, default=7, help="days of history per contract before expiry")
            s.add_argument("--strike-buffer", type=float, default=0.03, help="fraction beyond traded range")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "token":
        load_env()
        if args.new:
            if not totp_configured():
                raise SystemExit("TOTP not configured: set INDMONEY_CLIENT_ID, INDMONEY_MPIN, INDMONEY_TOTP_SECRET in .env")
            generate_token()
        print(token_status())
        return

    client, store = _client(), DataStore()

    if args.cmd == "fill-gaps":  # no INDmoney login needed
        fill_index_gaps(DataStore(), args.underlying, args.start, args.end, args.interval)
        return
    if args.cmd == "check":
        profile = client.profile() or {}
        name = " ".join(filter(None, [profile.get("first_name"), profile.get("last_name")]))
        print("Token OK. Logged in as:", name or "<unknown>")
        print("F&O enabled: NSE", profile.get("is_nse_fno_onboarded"), "| BSE", profile.get("is_bse_fno_onboarded"))
    elif args.cmd == "index":
        download_index(client, store, args.underlying, args.start, args.end, args.interval)
    else:
        expired = download_options(client, store, args.underlying, args.start, args.end, args.interval,
                                   args.days_before, args.strike_buffer)
        download_live_options(client, store, args.underlying, args.start, args.end, args.interval,
                              args.days_before, args.strike_buffer, already_expired=set(expired))


if __name__ == "__main__":
    main()
