"""Pull historical data from INDmoney into the local cache.

Usage:
  python -m data.downloader token [--new]   # show token status / force a new TOTP token
  python -m data.downloader check
  python -m data.downloader index   --underlying NIFTY --from 2025-01-01 --to 2025-03-31 --interval 1minute
  python -m data.downloader options --underlying NIFTY --from 2025-01-01 --to 2025-03-31 --interval 1minute

`options` downloads, for every expired weekly expiry in the range, only the
strikes the index actually traded near during that contract's last
`--days-before` days (+/- `--strike-buffer`), which keeps API calls small.
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, time, timedelta

from core.config import load_env
from data.indmoney_auth import generate_token, token_status, totp_configured
from data.indmoney_client import IndMoneyClient
from data.store import DataStore
from data.underlyings import get_underlying

log = logging.getLogger("downloader")

# The contracts endpoints document `segment=DERIVATIVE`; candles use NFO/BFO.
CONTRACTS_SEGMENT = "DERIVATIVE"


def _client() -> IndMoneyClient:
    load_env()
    return IndMoneyClient.from_env()


def _index_code(client: IndMoneyClient, store: DataStore, underlying: str) -> str:
    code = store.get_index_code(underlying)
    if code is None:
        u = get_underlying(underlying)
        code = client.index_scrip_code(u.index_name, u.index_exchange.value)
        store.set_index_code(underlying, code)
        log.info("Resolved %s index scrip code: %s", underlying, code)
    return code


def download_index(client, store, underlying: str, start: date, end: date, interval: str) -> None:
    u = get_underlying(underlying)
    code = _index_code(client, store, underlying)
    frames = client.historical([code], interval, datetime.combine(start, time.min),
                               datetime.combine(end + timedelta(days=1), time.min))
    df = frames.get(code)
    if df is None or df.empty:
        log.warning("No index candles returned for %s (%s)", underlying, code)
        return
    store.save_candles(interval, u.index_exchange.value, underlying, df)
    log.info("%s: saved %d index candles (%s -> %s)", underlying, len(df), df.index[0], df.index[-1])


def download_options(client, store, underlying: str, start: date, end: date, interval: str,
                     days_before: int, strike_buffer: float) -> None:
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
        symbols = [s for s in opts["trading_symbol"]
                   if not store.has_candles(interval, u.option_exchange.value, s)]
        if not symbols:
            continue
        frames = client.expired_historical(symbols, interval, w_start, w_end, u.option_exchange.value)
        for sym, df in frames.items():
            store.save_candles(interval, u.option_exchange.value, sym, df)
        log.info("%s %s: saved %d/%d option series (strikes %.0f-%.0f)",
                 underlying, expiry, len(frames), len(symbols), lo, hi)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("token", help="show token status; --new generates a fresh TOTP token")
    t.add_argument("--new", action="store_true")
    sub.add_parser("check", help="verify the access token works")
    for name in ("index", "options"):
        s = sub.add_parser(name)
        s.add_argument("--underlying", required=True, choices=["NIFTY", "SENSEX"])
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

    if args.cmd == "check":
        profile = client.profile() or {}
        print("Token OK. Logged in as:", profile.get("name") or profile.get("user_name") or "<unknown>")
    elif args.cmd == "index":
        download_index(client, store, args.underlying, args.start, args.end, args.interval)
    else:
        download_options(client, store, args.underlying, args.start, args.end, args.interval,
                         args.days_before, args.strike_buffer)


if __name__ == "__main__":
    main()
