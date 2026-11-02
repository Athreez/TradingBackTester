"""Environment / secrets loading. Secrets live only in git-ignored .env files."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


class Secret:
    """Wraps a secret so it never shows up in logs, reprs or tracebacks."""

    def __init__(self, value: str):
        self._value = value

    def get(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(****)"

    __str__ = __repr__


def load_env(*extra: Path) -> None:
    """Load the root .env plus any bot-specific .env files (earlier files win)."""
    for path in (*extra, ROOT / ".env"):
        if path.exists():
            load_dotenv(path, override=False)


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


def secret(name: str) -> Secret:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set. Add it to {ROOT / '.env'} (see .env.example).")
    return Secret(value)
