"""Thin wrapper over the INDstocks (INDmoney) REST API -- market data only.

Docs: https://api-docs.indstocks.com
Auth: raw access token in the `Authorization` header (see data/indmoney_auth.py).
Rate limit for data APIs: 5 req/s, 100k/day -> we stay under with a client-side limiter.
"""
from __future__ import annotations

import io
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import pandas as pd
import requests

from core.config import Secret

log = logging.getLogger(__name__)

BASE_URL = "https://api.indstocks.com"

# Max span of a single historical request per interval (from the docs).
MAX_WINDOW = {
    **{f"{m}minute": timedelta(days=7) for m in (1, 2, 3, 4, 5, 10, 15, 30)},
    **{f"{m}minute": timedelta(days=15) for m in (60, 120, 180, 240)},
    "1day": timedelta(days=365),
    "1week": timedelta(days=365),
    "1month": timedelta(days=365),
}
MAX_LIVE_SCRIPS = 5
MAX_EXPIRED_SYMBOLS = 50


class IndMoneyError(RuntimeError):
    pass


class _RateLimiter:
    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval


IST = timezone(timedelta(hours=5, minutes=30))


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:  # all internal times are naive IST, independent of this PC's timezone
        dt = dt.replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)


def _candles_to_df(candles: list[dict] | None) -> pd.DataFrame:
    df = pd.DataFrame(candles or [], columns=["ts", "o", "h", "l", "c", "v"])
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    # Epoch seconds -> naive IST timestamps (all internal times are naive IST).
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return df.set_index("ts").sort_index()


TokenProvider = Callable[[str | None], Secret]  # rejected token (or None) -> token to use


def _is_auth_failure(resp: requests.Response) -> bool:
    return resp.status_code == 401 or (resp.status_code == 403 and "token" in resp.text.lower())


class IndMoneyClient:
    def __init__(self, token_provider: TokenProvider, requests_per_second: float = 4.0, max_retries: int = 5):
        self._token_provider = token_provider
        self._session = requests.Session()
        self._session.headers["Authorization"] = token_provider(None).get()
        self._limiter = _RateLimiter(requests_per_second)
        self._max_retries = max_retries

    @classmethod
    def from_env(cls, **kwargs) -> "IndMoneyClient":
        from data.indmoney_auth import get_token
        return cls(lambda rejected: get_token(rejected=rejected), **kwargs)

    # ---- plumbing -------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        # Long-running bots: pick up the daily token (provider returns the cached one until it expires).
        self._session.headers["Authorization"] = self._token_provider(None).get()
        refreshed = False
        for attempt in range(self._max_retries):
            self._limiter.wait()
            resp = self._session.get(BASE_URL + path, params=params, timeout=30)
            if resp.status_code == 429 or resp.status_code >= 500:
                delay = 2 ** attempt
                log.warning("%s -> HTTP %s, retrying in %ss", path, resp.status_code, delay)
                time.sleep(delay)
                continue
            if _is_auth_failure(resp):
                if refreshed:  # refresh at most once per request, never loop on token generation
                    raise IndMoneyError(f"Unauthorized (HTTP {resp.status_code}) even after refreshing the token: "
                                        f"{resp.text[:300]}")
                log.warning("Access token rejected (HTTP %s); refreshing", resp.status_code)
                rejected = self._session.headers["Authorization"]
                self._session.headers["Authorization"] = self._token_provider(rejected).get()
                refreshed = True
                continue
            if not resp.ok:
                raise IndMoneyError(f"GET {path} failed: HTTP {resp.status_code}: {resp.text[:500]}")
            return resp
        raise IndMoneyError(f"GET {path} failed after {self._max_retries} retries")

    def _json(self, path: str, params: dict | None = None):
        body = self._get(path, params).json()
        if isinstance(body, dict) and body.get("success") is False:
            raise IndMoneyError(f"GET {path} error: {body}")
        return body.get("data") if isinstance(body, dict) else body

    # ---- account --------------------------------------------------------

    def profile(self) -> dict:
        return self._json("/user/profile")

    # ---- instruments ----------------------------------------------------

    def instruments(self, source: str) -> pd.DataFrame:
        """Instrument master CSV. source: equity | fno | mcx | index."""
        return pd.read_csv(io.StringIO(self._get("/market/instruments", {"source": source}).text))

    def index_scrip_code(self, index_name: str, exchange: str) -> str:
        df = self.instruments("index")
        # The `index` file's 2nd column is labelled SEGMENT but holds the index name.
        match = df[(df.iloc[:, 0] == exchange) & (df.iloc[:, 1].str.upper() == index_name.upper())]
        if match.empty:
            raise IndMoneyError(f"Index '{index_name}' on {exchange} not found in instrument master")
        return f"{exchange}_{match.iloc[0, 2]}"

    def expired_expiries(self, underlying: str, segment: str, expiry_from: date, expiry_to: date) -> list[date]:
        """Past expiries, max 1 year span per call."""
        out: set[date] = set()
        start = expiry_from
        while start <= expiry_to:
            end = min(start + timedelta(days=364), expiry_to)
            data = self._json("/market/instruments/expired/expiries", {
                "underlying": underlying, "segment": segment,
                "expiry_from": start.isoformat(), "expiry_to": end.isoformat(),
            })
            out.update(pd.to_datetime(d).date() for d in data or [])
            start = end + timedelta(days=1)
        return sorted(out)

    def expired_contracts(self, underlying: str, segment: str, expiry: date) -> pd.DataFrame:
        data = self._json("/market/instruments/expired/contracts", {
            "underlying": underlying, "segment": segment, "expiry": expiry.isoformat(),
        })
        return pd.DataFrame(data or [])

    # ---- candles --------------------------------------------------------

    def historical(self, scrip_codes: list[str], interval: str, start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
        """Candles for live instruments (e.g. index scrip codes like NSE_40000001)."""
        return self._candles(
            f"/market/historical/{interval}", "scrip-codes", scrip_codes, MAX_LIVE_SCRIPS,
            interval, start, end, {},
        )

    def expired_historical(self, trading_symbols: list[str], interval: str, start: datetime, end: datetime,
                           segment: str) -> dict[str, pd.DataFrame]:
        """Candles for expired F&O contracts. segment: NFO (NIFTY) or BFO (SENSEX)."""
        return self._candles(
            f"/market/historical/expired/{interval}", "trading-symbols", trading_symbols, MAX_EXPIRED_SYMBOLS,
            interval, start, end, {"segment": segment},
        )

    def _candles(self, path, key_param, keys, batch, interval, start, end, extra) -> dict[str, pd.DataFrame]:
        if interval not in MAX_WINDOW:
            raise ValueError(f"Unsupported interval {interval}. Use one of {list(MAX_WINDOW)}")
        window = MAX_WINDOW[interval]
        frames: dict[str, list[pd.DataFrame]] = {}
        for i in range(0, len(keys), batch):
            chunk = keys[i:i + batch]
            w_start = start
            while w_start < end:
                w_end = min(w_start + window, end)
                data = self._json(path, {
                    key_param: ",".join(chunk), "start_time": _ms(w_start), "end_time": _ms(w_end), **extra,
                })
                for k, v in (data or {}).items():
                    frames.setdefault(k, []).append(_candles_to_df(v.get("candles")))
                w_start = w_end
        return {k: pd.concat(v).pipe(lambda d: d[~d.index.duplicated()]).sort_index() for k, v in frames.items()}
