"""Telegram notifications from the paper bot: entries, exits, live-mirror results and a day-end summary.

Set TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID in .env; without them nothing
is sent. Messages go out from a background thread with a few retries, so a slow or failing Telegram never
holds up or breaks the bot.

  python -m paper_trader.notify chat-id   # after messaging your bot once: prints your chat id
  python -m paper_trader.notify test      # sends a test message
"""
from __future__ import annotations

import html
import logging
import queue
import sys
import threading
import time
from datetime import date

import requests

from core.config import env, load_env
from core.models import Fill, Side

log = logging.getLogger("notify")
API = "https://api.telegram.org/bot{token}/{method}"


def inr(x: float, signed: bool = False) -> str:
    s = f"{abs(x):,.0f}"
    return (("+" if x >= 0 else "-") if signed else ("-" if x < 0 else "")) + "Rs " + s


def short_name(symbol: str) -> str:
    """NIFTY26NOV1724550CE -> NIFTY 24550 CE"""
    for u in ("NIFTY", "SENSEX"):
        if symbol.startswith(u):
            return f"{u} {symbol[len(u) + 7:-2]} {symbol[-2:]}"
    return symbol


class Telegram:
    def __init__(self, token: str, chat_id: str, session: requests.Session | None = None):
        self.token, self.chat_id = token, chat_id
        self.session = session or requests.Session()
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._worker, name="telegram", daemon=True).start()

    @classmethod
    def from_env(cls) -> "Telegram | None":
        token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
        return cls(token, chat) if token and chat else None

    def send(self, text: str) -> None:
        self._q.put(text)

    def flush(self, timeout: float = 20) -> None:
        end = time.monotonic() + timeout
        while not self._q.empty() and time.monotonic() < end:
            time.sleep(0.2)

    def call(self, method: str, payload: dict, timeout: float = 15) -> dict:
        """A direct Bot API call (e.g. getUpdates for commands); returns its `result`, raises on failure."""
        body = self.session.post(API.format(token=self.token, method=method), json=payload, timeout=timeout).json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method}: {body.get('description')}")
        return body.get("result")

    def _post(self, text: str) -> bool:
        resp = self.session.post(API.format(token=self.token, method="sendMessage"), timeout=15, json={
            "chat_id": self.chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})
        return resp.ok

    def _worker(self) -> None:
        while True:
            text = self._q.get()
            for attempt in range(4):
                try:
                    if self._post(text):
                        break
                except Exception as e:  # network blip: retry, then give up on this message
                    log.debug("telegram send failed: %s", e)
                time.sleep(2 ** attempt)
            else:
                log.warning("telegram message dropped after retries: %s", text[:80])
            self._q.task_done()


class TradeNotifier:
    """Turns the bot's fills and day results into short Telegram messages."""

    def __init__(self, tg: Telegram, muted=lambda: False):
        self.tg = tg
        self.muted = muted  # /mute: trade messages stop, alerts still go out

    def _send(self, text: str) -> None:
        if not self.muted():
            self.tg.send(text)

    def fills(self, fills: list[Fill], all_fills: list[Fill], strategy, catch_up: bool = False) -> None:
        if not fills:
            return
        late = " <i>(caught up after a late start)</i>" if catch_up else ""
        entries = [f for f in fills if f.tag.split(":")[0] in ("hedge", "short")]
        exits = [f for f in fills if f not in entries]
        if entries:
            today = getattr(strategy, "today", None)
            head = f"🟢 <b>ENTRY {today[0] if today else ''}</b>{late}"
            vix, sl = getattr(strategy, "day_vix", None), getattr(strategy, "day_sl_pct", None)
            lines = [head, f"VIX {vix:.2f} · stop-loss {sl:.0%}" if vix and sl else ""]
            lines += [f"{f.side.value} {short_name(f.instrument.symbol)} × {f.quantity} @ {f.price:.2f}" for f in entries]
            credit = sum(f.quantity * f.price * (1 if f.side is Side.SELL else -1) for f in entries)
            lines.append(f"Net credit {inr(credit)}")
            self._send("\n".join(l for l in lines if l))
        for f in exits:
            kind = f.tag.split(":")[0]
            title = {"sl": "🔴 <b>STOP-LOSS</b>", "hedge-exit": "↩️ Hedge exit", "exit-time": "⏹ <b>EXIT 14:45</b>",
                     "sl-exit": "🔴 Exit after stop-loss", "expiry-settlement": "📅 Expiry settlement",
                     "manual-exit": "🛑 <b>Closed via /exitnow</b>"}.get(kind, f.tag)
            opened = [o for o in all_fills if o.instrument == f.instrument and o.side is not f.side
                      and o.ts.date() == f.ts.date() and o.ts <= f.ts]
            pnl = ""
            if opened:
                entry = opened[-1].price
                pnl = f" (entry {entry:.2f}, {inr((f.price - entry) * f.quantity * (1 if f.side is Side.SELL else -1), True)})"
            self._send(f"{title}{late}\n{f.side.value} {short_name(f.instrument.symbol)} × {f.quantity} @ {f.price:.2f}{pnl}")

    def live_event(self, tag: str, symbol: str, status: str, info: str) -> None:
        self._send(f"🏦 INDmoney {short_name(symbol)} [{tag}]: <b>{html.escape(status)}</b>"
                     + (f"\n{html.escape(info)}" if info else ""))

    def alert(self, text: str) -> None:
        self.tg.send(f"⚠️ <b>{html.escape(text)}</b>")

    def day_summary(self, day: date, row: dict, strategy, initial_capital: float, mirror_stats: str = "") -> None:
        if not row.get("fills"):
            note = getattr(strategy, "note", "") or "no trade"
            self._send(f"📊 <b>{day:%a %d %b}</b>: no trade\n{html.escape(note)}\nEquity {inr(row['equity'])}")
            return
        today = getattr(strategy, "today", None)
        lines = [f"📊 <b>{day:%a %d %b} {today[0] if today else ''}</b>",
                 f"Net <b>{inr(row['net'], True)}</b> (gross {inr(row['gross'], True)}, charges {inr(row['charges'])})",
                 f"{html.escape(getattr(strategy, 'note', '') or '')}",
                 f"Equity {inr(row['equity'])} · since start {inr(row['equity'] - initial_capital, True)}"]
        if mirror_stats:
            lines.append(mirror_stats)
        self._send("\n".join(l for l in lines if l))


def main() -> None:
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first (from @BotFather).")
    if cmd == "chat-id":
        updates = requests.get(API.format(token=token, method="getUpdates"), timeout=15).json().get("result", [])
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") or u["message"]["chat"].get("title")
                 for u in updates if "message" in u}
        if not chats:
            raise SystemExit("No messages yet: send any message to your bot in Telegram, then run this again.")
        for cid, name in chats.items():
            print(f"TELEGRAM_CHAT_ID={cid}   ({name})")
    elif cmd == "test":
        tg = Telegram.from_env()
        if tg is None:
            raise SystemExit("Set TELEGRAM_CHAT_ID too (python -m paper_trader.notify chat-id).")
        tg.send("✅ Trading bot notifications are working.")
        tg.flush()
        print("sent")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
