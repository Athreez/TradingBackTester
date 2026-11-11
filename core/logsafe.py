"""Keep secrets out of log files and the console.

A failed Telegram request raises an error whose text holds the request URL, and that URL contains
the bot token (api.telegram.org/bot<token>/...); urllib3 logs the same URL at DEBUG. RedactingFormatter
masks bot tokens, JWT-style access tokens and the values of the secret settings in .env in every
formatted line, tracebacks included.
"""
from __future__ import annotations

import logging
import os
import re

SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "INDMONEY_MPIN", "INDMONEY_TOTP_SECRET", "INDMONEY_ACCESS_TOKEN")
MASK = "***"
_PATTERNS = [
    (re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"), MASK),  # Telegram bot token
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), MASK),  # JWT access token
]
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    for name in SECRET_ENV:
        value = os.environ.get(name, "").strip()
        if len(value) >= 4:  # never mask trivially short values (would garble ordinary text)
            text = text.replace(value, MASK)
    return text


class RedactingFormatter(logging.Formatter):
    """Wraps a handler's own formatter (custom clocks etc. keep working) and masks its output."""

    def __init__(self, inner: logging.Formatter | None = None):
        super().__init__(FORMAT)
        self.inner = inner or logging.Formatter(FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        return redact(self.inner.format(record))


def protect_handlers(logger: logging.Logger | None = None) -> None:
    """Make every handler on `logger` (default: root) mask secrets, keeping its own format."""
    for h in (logger or logging.getLogger()).handlers:
        if not isinstance(h.formatter, RedactingFormatter):
            h.setFormatter(RedactingFormatter(h.formatter))
