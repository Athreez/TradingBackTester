"""INDmoney access token management.

Preferred: TOTP auto-login. Set INDMONEY_CLIENT_ID, INDMONEY_MPIN and
INDMONEY_TOTP_SECRET in .env; a token is generated on demand and cached in
.secrets/ (git-ignored) until it expires, because every new token invalidates
the previous one and generation is throttled (1/min, lockout after 5 bad codes).

Fallback: paste a dashboard token into INDMONEY_ACCESS_TOKEN.
"""
from __future__ import annotations

import json
import logging
import time
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


class AuthError(RuntimeError):
    pass


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


def _write_cache(path: Path, token: str, expires_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": token, "expires_at": expires_at.isoformat()}))


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

    expires_at = token_expiry(datetime.now(IST))
    _write_cache(cache_path, token, expires_at)
    log.info("Generated new INDmoney token, valid until %s", expires_at.strftime("%Y-%m-%d %H:%M IST"))
    return Secret(token), expires_at


def get_token(force_new: bool = False, cache_path: Path = CACHE_PATH) -> Secret:
    """A valid access token: cached TOTP token, freshly generated one, or the manual .env token."""
    if totp_configured():
        cached = None if force_new else _read_cache(cache_path)
        if cached and datetime.now(IST) < cached[1] - EXPIRY_MARGIN:
            return cached[0]
        return generate_token(cache_path)[0]
    if force_new:
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
