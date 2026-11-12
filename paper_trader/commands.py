"""Telegram commands for the paper bot (/help lists them).

A background thread long-polls Telegram for messages and answers only the chats in TELEGRAM_CHAT_IDS
(others get no reply; their chat id is logged once so it can be whitelisted).
Read-only commands answer from the files the bot already writes (state/live.json, logs/daily.csv, the
day's log, live_trader/state), so they never touch the trading loop. Control commands only set flags in
Controls (saved to state/controls.json); the trading loop applies them on its next pass.

Only one program can read a bot's messages: while this runs, `python -m paper_trader.notify chat-id`
elsewhere gets nothing (and would take these commands if it ran).
"""
from __future__ import annotations

import csv
import html
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("commands")

COMMANDS = [
    ("status", "bot state, open legs with live P&L, pending stop-losses"),
    ("today", "today's plan: index, expiry, VIX and the rule it gives, entry time"),
    ("pnl", "P&L today, this week, this month and since start"),
    ("history", "last N days, e.g. /history 10"),
    ("live", "INDmoney mirror: orders sent today and how INDmoney answered"),
    ("health", "token, data freshness, mirror and pause state"),
    ("log", "last N lines of today's log, e.g. /log 20"),
    ("pause", "skip new entries until /resume (open trades keep their stops)"),
    ("resume", "allow entries again"),
    ("exitnow", "close every open position now (asks for YES first)"),
    ("mirror", "/mirror on or /mirror off: INDmoney live orders"),
    ("mute", "stop trade messages (alerts still come)"),
    ("unmute", "trade messages back on"),
    ("help", "this list"),
]
CONFIRM_SECONDS = 60
STALE_UPDATE_SECONDS = 300  # commands older than this when the bot starts are ignored (no stale /exitnow)


def _inr(x: float, signed: bool = False) -> str:
    s = f"Rs {abs(x):,.0f}"
    return (("+" if x >= 0 else "-") if signed else ("-" if x < 0 else "")) + s


class Controls:
    """Switches set from Telegram and read by the trading loop. Saved so a restart keeps them."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {"paused": False, "mirror": None, "muted": False}  # mirror None = as LIVE_ORDERS says
        if path.exists():
            try:
                self._data.update(json.loads(path.read_text()))
            except ValueError:
                log.warning("ignoring unreadable %s", path)
        self.exit_requested = threading.Event()

    def get(self, key: str):
        with self._lock:
            return self._data[key]

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data))


@dataclass
class Paths:
    snapshot: Path  # paper_trader/state/live.json
    log_dir: Path  # paper_trader/logs
    live_dir: Path  # live_trader


class CommandHandler:
    """Turns a command into a reply. No Telegram here, so it is easy to test."""

    def __init__(self, controls: Controls, paths: Paths, now=datetime.now):
        self.controls, self.paths, self.now = controls, paths, now
        self._exit_asked: dict[str, datetime] = {}  # chat -> when it sent /exitnow (YES must come from that chat)
        self._chat = ""

    def handle(self, text: str, chat: str | int = "") -> str:
        self._chat = str(chat)
        parts = text.strip().split()
        if not parts:
            return ""
        if parts[0].upper() == "YES":
            return self._confirm_exit()
        cmd = parts[0].lstrip("/").split("@")[0].lower()  # /status@MyBot in groups
        arg = parts[1] if len(parts) > 1 else ""
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            return f"Unknown command {html.escape(parts[0])}. /help lists them."
        try:
            return fn(arg)
        except Exception as e:
            log.exception("command %s failed", cmd)
            return f"⚠️ /{cmd} failed: {html.escape(str(e))}"

    # ---- read-only ----------------------------------------------------------

    def _snap(self) -> dict:
        return json.loads(self.paths.snapshot.read_text()) if self.paths.snapshot.exists() else {}

    def _daily(self) -> list[dict]:
        path = self.paths.log_dir / "daily.csv"
        return list(csv.DictReader(path.open())) if path.exists() else []

    def _age(self, iso: str | None) -> str:
        if not iso:
            return "never"
        secs = (self.now() - datetime.fromisoformat(iso)).total_seconds()
        return f"{secs:.0f}s ago" if secs < 120 else f"{secs / 60:.0f} min ago" if secs < 7200 else f"{secs / 3600:.1f} h ago"

    def cmd_help(self, _="") -> str:
        return "<b>Commands</b>\n" + "\n".join(f"/{c} — {html.escape(d)}" for c, d in COMMANDS)

    cmd_start = cmd_help

    def cmd_status(self, _="") -> str:
        s = self._snap()
        if not s:
            return "No status yet: the bot has not written its snapshot."
        lines = [f"<b>{html.escape(s.get('phase') or '')}</b> · updated {self._age(s.get('updated_at'))}",
                 f"Day {s.get('day')} · {s.get('underlying') or 'no underlying'} · state {s.get('state')}",
                 html.escape(s.get("note") or "")]
        if self.controls.get("paused"):
            lines.append("⏸ Entries paused (/resume)")
        if s.get("positions"):
            lines.append("<b>Open legs</b>")
            total = 0.0
            for p in s["positions"]:
                u = p.get("unrealized")
                total += u or 0
                lines.append(f"{p['symbol']} {p['qty']:+d} @ {p['avg_price']} · last {p.get('ltp')} · "
                             f"{_inr(u, True) if u is not None else '?'}")
            lines.append(f"Unrealized {_inr(total, True)}")
        if s.get("open_orders"):
            lines.append("<b>Pending stop-losses</b>")
            for o in s["open_orders"]:
                lim = f" limit {o['limit']}" if o.get("limit") else ""
                lines.append(f"{o['side']} {o['symbol']} × {o['qty']} trigger {o.get('trigger')}{lim}")
        lines.append(f"Equity {_inr(s.get('equity', 0))}")
        return "\n".join(l for l in lines if l)

    def cmd_today(self, _="") -> str:
        s = self._snap()
        plan = s.get("plan") or {}
        if not plan:
            return (f"Day {s.get('day') or '?'}: plan not available yet (the bot works it out once candles arrive "
                    f"after 09:15).\n{html.escape(s.get('note') or '')}")
        lines = [f"<b>{plan.get('underlying')}</b> expiry {plan.get('expiry')} ({plan.get('dte')} days away)",
                 f"Entry {plan.get('entry_time')} · exit {plan.get('exit_time')}"]
        if plan.get("vix") is not None:
            lines.append(f"India VIX {plan['vix']:.2f} → {html.escape(plan.get('rule') or '')}")
        if plan.get("max_dte") is not None and plan.get("dte", 0) > plan["max_dte"]:
            lines.append(f"Skipped: expiry more than {plan['max_dte']} day(s) away")
        lines.append(f"Status: {html.escape(s.get('note') or '')}")
        if self.controls.get("paused"):
            lines.append("⏸ Entries paused (/resume)")
        return "\n".join(lines)

    def cmd_pnl(self, _="") -> str:
        s, rows = self._snap(), self._daily()
        equity = float(s.get("equity") or (rows[-1]["equity"] if rows else 0))
        start = float(s.get("initial_capital") or 0)
        today = self.now().date()

        def equity_before(day: date) -> float:
            before = [r for r in rows if date.fromisoformat(r["date"]) < day]
            return float(before[-1]["equity"]) if before else start

        week_start = today - timedelta(days=today.weekday())
        return "\n".join([
            f"Today {_inr(equity - equity_before(today), True)}",
            f"This week {_inr(equity - equity_before(week_start), True)}",
            f"This month {_inr(equity - equity_before(today.replace(day=1)), True)}",
            f"Since start {_inr(equity - start, True)} (on {_inr(start)})",
            f"Equity {_inr(equity)}"])

    def cmd_history(self, arg="") -> str:
        n = max(1, min(int(arg or 10), 30))
        rows = self._daily()[-n:]
        if not rows:
            return "No completed sessions yet."
        out = [f"{date.fromisoformat(r['date']):%a %d %b}  {'·' if int(r['fills']) == 0 else ('✅' if float(r['net']) > 0 else '❌')} "
               f"{_inr(float(r['net']), True) if int(r['fills']) else 'no trade'}" for r in rows]
        traded = [r for r in rows if int(r["fills"])]
        out.append(f"\n{len(traded)} traded, {sum(float(r['net']) > 0 for r in traded)} won, "
                   f"net {_inr(sum(float(r['net']) for r in rows), True)}")
        return "\n".join(out)

    def cmd_live(self, _="") -> str:
        s = self._snap()
        day = s.get("day") or self.now().date().isoformat()
        lines = [f"Mirror: {html.escape(s.get('mirror') or 'unknown')}"]
        state = self.paths.live_dir / "state" / f"mirror_{day.replace('-', '')}.json"
        if not state.exists():
            return "\n".join(lines + [f"No orders mirrored on {day}."])
        m = json.loads(state.read_text())
        for o in m["orders"]:
            last = list(o["statuses"].values())[-1] if o["statuses"] else "no answer yet"
            book = f" · book {o['book_price']:.2f}" if o.get("book_price") else ""
            lines.append(f"{o['tag']} {o['side']} {o['symbol']} × {o['qty']}: {html.escape(last)}{book}")
        if m.get("halted"):
            lines.append(f"Stopped: {html.escape(m['halted'])}")
        compare = self.paths.live_dir / "logs" / "compare.csv"
        if compare.exists():
            rows = [r for r in csv.DictReader(compare.open()) if r["ts"].startswith(day)]
            if rows:
                worse = sum(float(r["worse_by_rupees"]) for r in rows)
                lines.append(f"Live book vs paper fills: {'paper better by' if worse > 0 else 'live book better by'} "
                             f"{_inr(abs(worse))} over {len(rows)} fills")
        return "\n".join(lines)

    def cmd_health(self, _="") -> str:
        s = self._snap()
        lines = [f"Snapshot updated {self._age(s.get('updated_at'))} ({html.escape(s.get('phase') or '?')})",
                 f"Last candle {s.get('last_bar') or 'none'} ({self._age(s.get('last_bar'))})",
                 f"INDmoney token valid until {s.get('token_valid_until') or 'unknown'}",
                 f"Mirror: {html.escape(s.get('mirror') or 'unknown')}",
                 f"Entries {'⏸ paused' if self.controls.get('paused') else 'allowed'} · "
                 f"messages {'🔇 muted' if self.controls.get('muted') else 'on'}"]
        return "\n".join(lines)

    def cmd_log(self, arg="") -> str:
        n = max(1, min(int(arg or 20), 60))
        logs = sorted(self.paths.log_dir.glob("paper_*.log"))
        if not logs:
            return "No log yet."
        tail = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        return f"<b>{logs[-1].name}</b>\n<pre>{html.escape(chr(10).join(l[11:] for l in tail))[-3500:]}</pre>"

    # ---- control ------------------------------------------------------------

    def cmd_pause(self, _="") -> str:
        self.controls.set("paused", True)
        return "⏸ New entries paused. Open positions keep their stop-losses and the 14:45 exit. /resume to undo."

    def cmd_resume(self, _="") -> str:
        self.controls.set("paused", False)
        return "▶️ Entries allowed again (a day already skipped while paused stays skipped)."

    def cmd_exitnow(self, _="") -> str:
        self._exit_asked[self._chat] = self.now()
        return f"🛑 Close ALL open positions at market now? Reply <b>YES</b> within {CONFIRM_SECONDS}s."

    def _confirm_exit(self) -> str:
        asked = self._exit_asked.pop(self._chat, None)
        if asked is None or (self.now() - asked).total_seconds() > CONFIRM_SECONDS:
            return "Nothing to confirm (send /exitnow first)."
        self.controls.exit_requested.set()
        return "🛑 Closing everything on the next bar; you'll get the exit messages."

    def cmd_mirror(self, arg="") -> str:
        if arg.lower() not in ("on", "off"):
            return "Use /mirror on or /mirror off."
        self.controls.set("mirror", arg.lower() == "on")
        return (f"INDmoney mirroring turned {arg.lower()}. "
                + ("Orders are sent again from the next one (only while the account is empty)."
                   if arg.lower() == "on" else "No more live orders are sent; open ones are cancelled at day end."))

    def cmd_mute(self, _="") -> str:
        self.controls.set("muted", True)
        return "🔇 Trade messages muted. Alerts still come. /unmute to undo."

    def cmd_unmute(self, _="") -> str:
        self.controls.set("muted", False)
        return "🔔 Trade messages back on."


class CommandBot:
    """Long-polls Telegram and answers commands from the one allowed chat."""

    def __init__(self, tg, handler: CommandHandler, blocked_path: Path | None = None):
        self.tg, self.handler = tg, handler
        self.offset: int | None = None
        self._blocked_seen: set[str] = set()
        self.blocked_path = blocked_path  # state/blocked_chats.json: chats that wrote but aren't whitelisted

    def start(self) -> None:
        try:
            self.tg.call("setMyCommands", {"commands": [{"command": c, "description": d[:256]} for c, d in COMMANDS]})
        except Exception as e:
            log.warning("could not register the command menu: %s", e)
        threading.Thread(target=self._run, name="telegram-commands", daemon=True).start()

    def _run(self) -> None:
        started = time.time()
        while True:
            try:
                updates = self.tg.call("getUpdates", {"timeout": 25, **({"offset": self.offset} if self.offset else {})},
                                       timeout=35)
            except Exception as e:
                log.debug("getUpdates failed: %s", e)
                time.sleep(10)
                continue
            for u in updates or []:
                self.offset = u["update_id"] + 1
                self.process(u, started)

    def process(self, update: dict, started: float) -> None:
        msg = update.get("message") or {}
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id"))
        if not self.tg.allowed(chat_id):  # not whitelisted: no reply; logged once so it can be added
            if chat_id not in self._blocked_seen:
                self._blocked_seen.add(chat_id)
                who = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
                log.warning("ignored Telegram message from chat %s (%s): not in TELEGRAM_CHAT_IDS", chat_id, who)
                self._record_blocked(chat_id, who, msg.get("date", 0))
            return
        if msg.get("date", 0) < started - STALE_UPDATE_SECONDS:
            return  # sent long before the bot started: don't act on it now
        reply = self.handler.handle(msg.get("text") or "", chat=chat_id)
        if reply:
            self.tg.send(reply, chat_id=chat_id)  # answer only the chat that asked

    def _record_blocked(self, chat_id: str, who: str, ts: int) -> None:
        """Keep the ids of chats that tried, so one can be copied into TELEGRAM_CHAT_IDS."""
        if self.blocked_path is None:
            return
        try:
            seen = json.loads(self.blocked_path.read_text()) if self.blocked_path.exists() else {}
            seen[chat_id] = {"name": who, "last_message": datetime.fromtimestamp(ts).isoformat(timespec="seconds")}
            self.blocked_path.parent.mkdir(parents=True, exist_ok=True)
            self.blocked_path.write_text(json.dumps(seen, indent=1))
        except (OSError, ValueError) as e:
            log.warning("could not record blocked chat: %s", e)
