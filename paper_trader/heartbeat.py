"""Dead man's switch: while it runs, the bot pings a healthchecks.io check; when the pings stop (PC off,
crash, hang, internet down) healthchecks.io raises the alarm, e.g. on Telegram. A bot can't report its own
death, so the alarm has to live somewhere else.

.env: HEALTHCHECK_URL=https://hc-ping.com/<uuid>   (empty = off)

Setup at healthchecks.io (free): New check -> Schedule "Cron", expression `* 9-15 * * 1-5`, time zone
Asia/Kolkata, grace time 3 minutes (= alert if no ping for 3 minutes on weekdays 09:00-16:00);
Integrations -> Telegram. The bot also reports a failure (an immediate alert) when it is running but
something is wrong, e.g. candles stopped arriving mid-session; the next good ping clears it.
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from core.config import env

log = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, url: str, every: float = 60, session: requests.Session | None = None):
        self.url = url.rstrip("/")
        self.every = every
        self.session = session or requests.Session()
        self._last_sent = 0.0
        self._last_ok: bool | None = None

    @classmethod
    def from_env(cls) -> "Heartbeat | None":
        url = env("HEALTHCHECK_URL")
        return cls(url) if url else None

    def beat(self, problem: str | None = None) -> None:
        """Called often; sends at most one ping a minute, plus at once whenever ok/failing changes."""
        ok = problem is None
        now = time.monotonic()
        if ok == self._last_ok and now - self._last_sent < self.every:
            return
        if ok != self._last_ok:
            log.info("heartbeat: %s", "ok" if ok else f"failing: {problem}")
        self._last_sent, self._last_ok = now, ok
        threading.Thread(target=self._send, args=(ok, problem or "ok"), daemon=True).start()

    def _send(self, ok: bool, text: str) -> None:
        try:
            self.session.post(self.url if ok else f"{self.url}/fail", data=text[:1000].encode(), timeout=10)
        except Exception as e:  # never let monitoring break the bot; a missed ping just alerts later
            log.debug("heartbeat ping failed: %s", e)
