"""Telegram commands, tested on files and fakes only: nothing here talks to Telegram or INDmoney."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from paper_trader.commands import COMMANDS, CommandBot, CommandHandler, Controls, Paths
from paper_trader.notify import Telegram, TradeNotifier, parse_chat_ids

NOW = datetime(2026, 11, 12, 11, 0)


@pytest.fixture
def setup(tmp_path):
    paths = Paths(tmp_path / "state" / "live.json", tmp_path / "logs", tmp_path / "live_trader")
    paths.snapshot.parent.mkdir(parents=True)
    paths.log_dir.mkdir()
    paths.snapshot.write_text(json.dumps({
        "updated_at": "2026-11-12T10:59:30", "phase": "trading session", "day": "2026-11-12",
        "last_bar": "2026-11-12T10:58:00", "underlying": "SENSEX", "state": "OPEN",
        "note": "entered: ATM 78700; CE stop-loss hit at 09:43", "equity": 556000.0, "initial_capital": 500000.0,
        "positions": [{"symbol": "SENSEX26NOV1278700PE", "qty": -20, "avg_price": 298.4, "ltp": 200.0, "unrealized": 1968.0}],
        "open_orders": [{"symbol": "SENSEX26NOV1278700PE", "side": "BUY", "type": "STOP_LIMIT", "qty": 20,
                         "trigger": 387.9, "limit": 407.3, "tag": "sl:PE"}],
        "token_valid_until": "2026-11-13T07:00", "mirror": "on",
        "plan": {"underlying": "SENSEX", "expiry": "2026-11-12", "dte": 0, "vix": 15.17, "rule": "trade, stop-loss 30%",
                 "max_dte": None, "entry_time": "09:22", "exit_time": "14:45"}}))
    (paths.log_dir / "daily.csv").write_text(
        "date,fills,gross,charges,net,equity\n"
        "2026-11-06,8,4958.19,155.09,4803.10,550537.56\n"
        "2026-11-09,8,3718.82,126.01,3592.81,554130.37\n"
        "2026-11-11,0,0,0,0,554130.37\n")
    (paths.log_dir / "paper_20261112.log").write_text(
        "\n".join(f"2026-11-12 10:{m:02d}:00,000 INFO paper: line {m} <x>" for m in range(30)), encoding="utf-8")
    controls = Controls(tmp_path / "state" / "controls.json")
    clock = {"now": NOW}
    return CommandHandler(controls, paths, now=lambda: clock["now"]), controls, paths, clock


def test_help_lists_every_command(setup):
    h, *_ = setup
    text = h.handle("/help")
    assert all(f"/{c} — " in text for c, _ in COMMANDS)
    assert h.handle("/start") == text
    assert "Unknown command" in h.handle("/foo")


def test_read_only_commands(setup):
    h, *_ = setup
    s = h.handle("/status")
    assert "SENSEX" in s and "-20 @ 298.4" in s and "+Rs 1,968" in s and "trigger 387.9 limit 407.3" in s
    assert "updated 30s ago" in s
    t = h.handle("/today")
    assert "SENSEX</b> expiry 2026-11-12 (0 days away)" in t and "VIX 15.17 → trade, stop-loss 30%" in t
    p = h.handle("/pnl")
    assert "Today +Rs 1,870" in p and "This week +Rs 5,462" in p and "Since start +Rs 56,000" in p
    hist = h.handle("/history 2")
    assert "Mon 09 Nov  ✅ +Rs 3,593" in hist and "no trade" in hist and "1 traded, 1 won" in hist
    hl = h.handle("/health")
    assert "token valid until 2026-11-13T07:00" in hl and "Mirror: on" in hl and "allowed" in hl
    lg = h.handle("/log 3")
    assert "line 29 &lt;x&gt;" in lg and "line 26" not in lg  # last 3 lines, HTML-escaped


def test_live_reads_the_mirror_state(setup):
    h, _, paths, _ = setup
    assert "No orders mirrored" in h.handle("/live")
    state = paths.live_dir / "state"
    state.mkdir(parents=True)
    (state / "mirror_20261112.json").write_text(json.dumps({"halted": None, "seen": [], "fills_seen": 0, "orders": [
        {"tag": "hedge:CE", "side": "BUY", "symbol": "SENSEX26NOV1280300CE", "qty": 20,
         "statuses": {"DRV-1": "FAILED"}, "book_price": 9.15}]}))
    (paths.live_dir / "logs").mkdir()
    (paths.live_dir / "logs" / "compare.csv").write_text(
        "ts,tag,symbol,side,qty,paper_fill,book_price_at_send,worse_by_per_unit,worse_by_rupees\n"
        "2026-11-12 09:22:00,hedge:CE,X,BUY,20,9.10,9.15,0.05,1.0\n")
    text = h.handle("/live")
    assert "hedge:CE BUY SENSEX26NOV1280300CE × 20: FAILED · book 9.15" in text
    assert "paper better by Rs 1 over 1 fills" in text


def test_switches_persist(setup, tmp_path):
    h, controls, *_ = setup
    h.handle("/pause")
    h.handle("/mute")
    assert "Use /mirror on" in h.handle("/mirror maybe")
    h.handle("/mirror off")
    again = Controls(tmp_path / "state" / "controls.json")  # as after a restart
    assert again.get("paused") and again.get("muted") and again.get("mirror") is False
    assert "⏸ Entries paused" in h.handle("/status")
    h.handle("/resume")
    h.handle("/unmute")
    assert not controls.get("paused") and not controls.get("muted")


def test_exitnow_needs_yes_within_a_minute(setup):
    h, controls, _, clock = setup
    assert "Nothing to confirm" in h.handle("YES")
    h.handle("/exitnow")
    clock["now"] = NOW + timedelta(seconds=61)
    assert "Nothing to confirm" in h.handle("yes")
    assert not controls.exit_requested.is_set()
    h.handle("/exitnow")
    assert "Closing everything" in h.handle("YES")
    assert controls.exit_requested.is_set()


def test_exitnow_yes_must_come_from_the_chat_that_asked(setup):
    h, controls, _, _ = setup
    h.handle("/exitnow", chat="1000000001")
    assert "Nothing to confirm" in h.handle("YES", chat="1000000002")
    assert not controls.exit_requested.is_set()
    assert "Closing everything" in h.handle("YES", chat="1000000001")


class FakeTg:
    chat_ids = ["1000000001", "1000000002"]

    def __init__(self):
        self.sent = []

    def allowed(self, chat_id):
        return str(chat_id) in self.chat_ids

    def send(self, text, chat_id=None):
        self.sent.append((chat_id, text))


def test_only_answers_whitelisted_chats_and_fresh_messages(setup, caplog, tmp_path):
    h, *_ = setup
    tg = FakeTg()
    bot = CommandBot(tg, h, blocked_path=tmp_path / "blocked_chats.json")
    started = 1_800_000_000
    bot.process({"message": {"chat": {"id": 111, "first_name": "Stranger"}, "date": started, "text": "/status"}}, started)
    bot.process({"message": {"chat": {"id": 111}, "date": started, "text": "/pnl"}}, started)
    bot.process({"message": {"chat": {"id": 1000000001}, "date": started - 3600, "text": "/exitnow"}}, started)
    assert tg.sent == []
    assert [r.getMessage() for r in caplog.records].count(
        "ignored Telegram message from chat 111 (Stranger): not in TELEGRAM_CHAT_IDS") == 1  # logged once
    assert json.loads((tmp_path / "blocked_chats.json").read_text())["111"]["name"] == "Stranger"
    bot.process({"message": {"chat": {"id": 1000000001}, "date": started + 5, "text": "/help"}}, started)
    bot.process({"message": {"chat": {"id": 1000000002}, "date": started + 6, "text": "/help"}}, started)
    assert [c for c, _ in tg.sent] == ["1000000001", "1000000002"]  # each reply goes to the chat that asked
    assert "/exitnow" in tg.sent[0][1]


@pytest.mark.parametrize("raw, ids", [
    ("[1000000001, -1000000002]", ["1000000001", "-1000000002"]),
    ("1000000001,1000000002", ["1000000001", "1000000002"]),
    ('["1000000001"]', ["1000000001"]),
    ("", []), (None, []), ("[]", []),
])
def test_parse_chat_ids(raw, ids):
    assert parse_chat_ids(raw) == ids


def test_notifications_go_to_every_whitelisted_chat():
    posted = []

    class Session:
        def post(self, url, json, timeout):
            posted.append(json["chat_id"])
            return type("R", (), {"ok": True})()

    tg = Telegram("123:abc", ["1000000001", "1000000002"], session=Session())
    tg.send("hello")
    tg.send("only you", chat_id="1000000002")
    tg._q.join()
    assert posted == ["1000000001", "1000000002", "1000000002"]
    assert Telegram("123:abc", "1000000001", session=Session()).chat_ids == ["1000000001"]  # single id still works


def test_mute_keeps_alerts(setup):
    _, controls, *_ = setup
    tg = FakeTg()
    n = TradeNotifier(tg, muted=lambda: controls.get("muted"))
    controls.set("muted", True)
    n.live_event("hedge:CE", "NIFTY26NOV1724550CE", "FAILED", "margin")
    n.alert("account has money")
    assert len(tg.sent) == 1 and tg.sent[0][0] is None and "account has money" in tg.sent[0][1]  # None = all chats


def test_runner_applies_controls_without_logging_in(setup, tmp_path):
    """PaperBot's control handling, on a bare object: constructing a real PaperBot would log in to INDmoney."""
    from paper_trader.runner import MIRROR_OFF, PaperBot
    _, controls, *_ = setup
    calls = []
    bot = PaperBot.__new__(PaperBot)
    bot.controls, bot.notifier = controls, None
    bot.strategy = SimpleNamespace(state="OPEN", note="entered")
    bot.broker = SimpleNamespace(positions=lambda: {"x": 1}, open_orders=lambda: [])
    bot.mirror = SimpleNamespace(halted=None)
    bot._make_mirror = lambda feed, today: calls.append("make")
    ctx = SimpleNamespace(cancel_all=lambda: calls.append("cancel_all"),
                          square_off_all=lambda tag: calls.append(tag))

    controls.set("paused", True)
    controls.set("mirror", False)
    bot._apply_controls(ctx, None, date(2026, 11, 12))
    assert bot.strategy.paused and bot.mirror.halted == MIRROR_OFF and calls == []

    controls.set("mirror", True)
    controls.exit_requested.set()
    bot._apply_controls(ctx, None, date(2026, 11, 12))
    assert bot.mirror.halted is None and calls == ["cancel_all", "manual-exit"]
    assert bot.strategy.state == "DONE" and "/exitnow" in bot.strategy.note

    bot.mirror.halted = "INDmoney reports Rs 5,000 available"  # a safety stop is never undone by /mirror on
    bot._apply_controls(ctx, None, date(2026, 11, 12))
    assert bot.mirror.halted.startswith("INDmoney reports")
