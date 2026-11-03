"""INDmoney access token management.

Preferred: TOTP auto-login. Set INDMONEY_CLIENT_ID, INDMONEY_MPIN and
INDMONEY_TOTP_SECRET in .env; a token is generated on demand and cached in
.secrets/ (git-ignored) until it expires, because every new token invalidates
the previous one and generation is throttled (1/min, lockout after 5 bad codes).

Several processes (paper bot, downloader, scripts) share one login and one
cache file. INDmoney keeps only ONE TOTP token alive, so a process whose token
is rejected first re-reads the cache (someone else may already have replaced
it) and generates under a file lock. A token rejected within MIN_TOKEN_AGE of
being generated means another program or the website is using the login: that
raises LoginConflict instead of generating again, so two processes can never
cancel each other's tokens in a loop.

Fallback: paste a dashboard token into INDMONEY_ACCESS_TOKEN.
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyotp
import requests

from core.config import ROOT, Secret, env, load_env, secret

log = logging.getLogger(__name__)

GENERATE_URL = "https://api.indstocks.com/generate/token"
CACHE_PATH = ROOT / ".secrets" / "indmoney_token.json"
IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY_MARGIN = timedelta(minutes=10)
MIN_TOKEN_AGE = timedelta(minutes=2)  # a younger token being rejected = someone else is using the login


class AuthError(RuntimeError):
    pass


class LoginConflict(AuthError):
    """A freshly generated token was rejected: another program / the website took over the login."""


@contextmanager
def _file_lock(path: Path, timeout: float = 30):
    """Cross-process lock so only one process generates a token at a time."""
    lock = path.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise AuthError("Timed out waiting for another process to finish generating a token")
                time.sleep(0.2)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)


def totp_configured() -> bool:
    load_env()
    return all(env(k) for k in ("INDMONEY_CLIENT_ID", "INDMONEY_MPIN", "INDMONEY_TOTP_SECRET"))


def token_expiry(generated_at: datetime) -> datetime:
    """Docs say 24h; the dashboard says until 7 AM next morning (SEBI). Use whichever is earlier."""
    next_7am = generated_at.replace(hour=7, minute=0, second=0, microsecond=0)
    if next_7am <= generated_at:
        next_7am += timedelta(days=1)
    return min(generated_at + timedelta(hours=24), next_7am)


def _read_cache(path: Path) -> tuple[Secret, datetime] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return Secret(data["token"]), datetime.fromisoformat(data["expires_at"])
    except (ValueError, KeyError):
        return None


def _cache_generated_at(path: Path) -> datetime | None:
    try:
        return datetime.fromisoformat(json.loads(path.read_text())["generated_at"])
    except (FileNotFoundError, ValueError, KeyError):
        return None


def _write_cache(path: Path, token: str, expires_at: datetime, generated_at: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"token": token, "expires_at": expires_at.isoformat(),
            "generated_at": (generated_at or datetime.now(IST)).isoformat()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def _current_totp(totp: pyotp.TOTP) -> str:
    # Avoid sending a code that rolls over in flight (a rejected code counts toward lockout).
    remaining = totp.interval - time.time() % totp.interval
    if remaining < 3:
        time.sleep(remaining + 0.5)
    return totp.now()


def generate_token(cache_path: Path = CACHE_PATH) -> tuple[Secret, datetime]:
    """Create a new token via TOTP. Invalidates any previously issued token."""
    load_env()
    client_id = secret("INDMONEY_CLIENT_ID")
    mpin = secret("INDMONEY_MPIN")
    try:
        totp = pyotp.TOTP(secret("INDMONEY_TOTP_SECRET").get().replace(" ", "").upper())
        code = _current_totp(totp)
    except Exception as e:  # malformed base32 secret
        raise AuthError(f"INDMONEY_TOTP_SECRET is not a valid TOTP key: {type(e).__name__}") from None

    resp = requests.post(
        GENERATE_URL,
        headers={"x-api-key": client_id.get(), "Content-Type": "application/json"},
        json={"mpin": mpin.get(), "totp": code},
        timeout=30,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    token = (body.get("data") or {}).get("token") if isinstance(body.get("data"), dict) else body.get("token")
    if not resp.ok or not token:
        # Never retry automatically: repeated failures lock the account out of token generation.
        msg = body.get("message") or body.get("debug_info") or resp.text[:300]
        raise AuthError(f"Token generation failed (HTTP {resp.status_code}): {msg}. "
                        "Check MPIN / TOTP secret / Client ID and that this PC's clock is synced.")

    now = datetime.now(IST)
    expires_at = token_expiry(now)
    _write_cache(cache_path, token, expires_at, generated_at=now)
    log.info("Generated new INDmoney token, valid until %s", expires_at.strftime("%Y-%m-%d %H:%M IST"))
    return Secret(token), expires_at


def get_token(force_new: bool = False, cache_path: Path = CACHE_PATH, rejected: str | None = None) -> Secret:
    """A valid access token: cached TOTP token, freshly generated one, or the manual .env token.

    rejected: the token the API just refused. If the cache already holds a different (newer) token,
    that one is returned; otherwise a new one is generated -- unless the rejected token is younger
    than MIN_TOKEN_AGE, which raises LoginConflict (never fight another holder of the login).
    """
    if totp_configured():
        def usable(c):
            return c and datetime.now(IST) < c[1] - EXPIRY_MARGIN

        cached = _read_cache(cache_path)
        if not force_new and rejected is None and usable(cached):
            return cached[0]
        with _file_lock(cache_path):
            cached = _read_cache(cache_path)  # another process may have refreshed while we waited
            if rejected is not None and usable(cached) and cached[0].get() != rejected:
                return cached[0]
            if not force_new and rejected is None and usable(cached):
                return cached[0]
            born = _cache_generated_at(cache_path)
            if (rejected is not None and cached and cached[0].get() == rejected and born
                    and datetime.now(IST) - born < MIN_TOKEN_AGE):
                raise LoginConflict(
                    f"INDmoney rejected a token generated {(datetime.now(IST) - born).seconds}s ago: another "
                    "program, PC or the indstocks.com website is using this login (only one token can be "
                    "active). Stop the other one; the bot retries in a couple of minutes.")
            return generate_token(cache_path)[0]
    if force_new or rejected is not None:
        raise AuthError("Manual INDMONEY_ACCESS_TOKEN was rejected/expired. Paste a new one into .env, "
                        "or configure TOTP (INDMONEY_CLIENT_ID / INDMONEY_MPIN / INDMONEY_TOTP_SECRET).")
    return secret("INDMONEY_ACCESS_TOKEN")


def token_status(cache_path: Path = CACHE_PATH) -> str:
    if not totp_configured():
        return "Using manual INDMONEY_ACCESS_TOKEN from .env (TOTP not configured)."
    cached = _read_cache(cache_path)
    if not cached:
        return "TOTP configured; no cached token yet."
    return f"TOTP configured; cached token valid until {cached[1].strftime('%Y-%m-%d %H:%M IST')}."
