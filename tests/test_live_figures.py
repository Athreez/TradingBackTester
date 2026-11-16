"""INDmoney's own figures for the live bot (live_trader/figures.py) and where they are shown: Telegram commands
and the dashboard's Live view. Responses are shaped like INDmoney's /funds and /portfolio/positions docs."""
import json
from datetime import date, datetime

import pytest

from live_trader import figures as ind
from paper_trader.commands import CommandHandler, Controls, Paths

AT = datetime(2026, 11, 16, 11, 30)
FUNDS = {"sod_balance": 225000, "detailed_avl_balance": {"option_sell": 61234.5, "future": 61234.5},
         "withdrawal_balance": 0, "funds_added": 225000, "funds_withdrawn": 0,
         "realized_pnl": 1500.0, "unrealized_pnl": -320.25, "brokerage": 40, "eq_charges": 0, "fno_charges": 61.3}
POSITIONS = [
    {"security_id": "1", "symbol": "NIFTY", "segment": "DERIVATIVE", "product": "INTRADAY",
     "drv_expiry_date": "11/17/2026 14:30", "drv_option_type": "CE", "drv_strike_price": 25050,
     "net_qty": -65, "avg_price": 120.5, "buy_qty": 0, "buy_avg": 0, "sell_qty": 65, "sell_avg": 120.5, "realized_profit": 0},
    {"security_id": "2", "symbol": "NIFTY", "segment": "DERIVATIVE", "product": "INTRADAY",
     "drv_expiry_date": "11/17/2026 14:30", "drv_option_type": "PE", "drv_strike_price": 25050,
     "net_qty": 0, "avg_price": 0, "buy_qty": 65, "buy_avg": 150, "sell_qty": 65, "sell_avg": 127, "realized_profit": -1495},
]


def test_figures_are_indmoneys_numbers():
    f = ind.figures(FUNDS, POSITIONS, AT)
    assert f["available"] == 61234.5 and f["realized"] == 1500 and f["unrealized"] == -320.25
    assert f["net"] == pytest.approx(1500 - 320.25 - 40 - 61.3)
    assert [p["contract"] for p in f["positions"]] == ["NIFTY 25050 CE 17 Nov", "NIFTY 25050 PE 17 Nov"]
    assert f["positions"][0]["qty"] == -65 and f["positions"][1]["realized"] == -1495


def test_figures_tolerate_missing_fields():
    f = ind.figures({}, [{"security_id": "9", "net_qty": "5"}], AT)
    assert f["net"] == 0 and f["positions"][0]["contract"] == "9" and f["positions"][0]["qty"] == 5


def test_daily_record_and_total(tmp_path):
    f = ind.figures(FUNDS, POSITIONS, AT)
    ind.append(tmp_path, ind.DAILY, ind.daily_row(date(2026, 11, 13), {**f, "net": 500.0}, traded=True))
    ind.append(tmp_path, ind.DAILY, ind.daily_row(date(2026, 11, 13), {**f, "net": 700.0}, traded=True))  # rewrite wins
    assert [r["net"] for r in ind.read_daily(tmp_path)] == ["700.0"]
    assert ind.total_since_start(tmp_path, f, date(2026, 11, 16)) == pytest.approx(700 + f["net"])  # today not recorded
    ind.append(tmp_path, ind.DAILY, ind.daily_row(date(2026, 11, 16), f, traded=True))
    assert ind.total_since_start(tmp_path, f, date(2026, 11, 16)) == pytest.approx(700 + f["net"])  # not counted twice


@pytest.fixture
def live_handler(tmp_path):
    f = ind.figures(FUNDS, POSITIONS, AT)
    snap = tmp_path / "live.json"
    snap.write_text(json.dumps({"mode": "live", "phase": "trading session", "updated_at": AT.isoformat(),
                                "day": "2026-11-16", "state": "OPEN", "equity": 999999, "initial_capital": 225000,
                                "positions": [], "open_orders": [], "indmoney": f,
                                "start_capital": 225000, "capital": 225800}))
    ind.append(tmp_path, ind.DAILY, ind.daily_row(date(2026, 11, 13), {**f, "net": 800.0}, traded=True))
    return CommandHandler(Controls(tmp_path / "controls.json"), Paths(snap, tmp_path, tmp_path), now=lambda: AT), f


def test_live_pnl_shows_indmoney_figures_only(live_handler):
    h, f = live_handler
    out = h.handle("/pnl")
    assert "INDmoney" in out and "Rs 1,078" in out and "realized +Rs 1,500" in out  # today's net from /funds
    assert "Since start +Rs 1,878" in out and "Capital Rs 225,800" in out
    assert "61,234" not in out  # INDmoney's margin figure is not the capital
    assert "999,999" not in out  # the bot's own equity is never shown


def test_live_status_and_history(live_handler):
    h, _ = live_handler
    status = h.handle("/status")
    assert "NIFTY 25050 CE 17 Nov -65 @ 120.50" in status and "INDmoney today +Rs 1,078" in status
    assert "Equity" not in status
    hist = h.handle("/history")
    assert "Fri 13 Nov" in hist and "+Rs 800" in hist and "(INDmoney)" in hist


def test_dashboard_live_view_uses_indmoney(tmp_path, monkeypatch):
    """Render the Live view against a fake live_trader folder: INDmoney's figures, never the bot's equity."""
    from pathlib import Path

    from streamlit.testing.v1 import AppTest
    live = tmp_path / "live_trader"
    (live / "state").mkdir(parents=True)
    f = ind.figures(FUNDS, POSITIONS, AT)
    (live / "state" / "live.json").write_text(json.dumps({
        "mode": "live", "phase": "trading session", "updated_at": AT.isoformat(), "strategy": "vix_iron_fly",
        "day": "2026-11-16", "state": "OPEN", "equity": 999999, "initial_capital": 225000, "underlying": "NIFTY",
        "spot": 25050, "positions": [], "open_orders": [], "note": "entered", "indmoney": f}))
    ind.append(live / "logs", ind.DAILY, ind.daily_row(date(2026, 11, 13), {**f, "net": 800.0}, traded=True))
    ind.append(live / "logs", ind.INTRADAY, {"ts": "2026-11-16T11:30:00", "net": f["net"], "realized": 1500,
                                             "unrealized": -320.25, "charges": 101.3})
    monkeypatch.setenv("LIVE_TRADER_DIR", str(live))
    monkeypatch.setenv("PAPER_TRADER_DIR", str(tmp_path / "paper"))
    at = AppTest.from_file(str(Path(__file__).parent.parent / "paper_trader" / "dashboard.py"), default_timeout=60)
    at.query_params["view"] = "live"
    at.run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Capital"] == "₹2,25,800.00"  # 2,25,000 start + the one closed day's net, not INDmoney's margin
    assert metrics["Today's P&L"] == f"+₹{f['net']:,.2f}"
    assert metrics["Total P&L"] == f"+₹{800 + f['net']:,.2f}"
    assert "Equity" not in metrics and all("999" not in v for v in metrics.values())


def test_capital_moves_only_with_closed_days(tmp_path):
    f = ind.figures(FUNDS, POSITIONS, AT)
    assert ind.capital(tmp_path, 225000) == 225000
    ind.append(tmp_path, ind.DAILY, ind.daily_row(date(2026, 11, 13), {**f, "net": 1200.0}, traded=True))
    ind.append(tmp_path, ind.DAILY, ind.daily_row(date(2026, 11, 16), {**f, "net": -450.5}, traded=True))
    assert ind.capital(tmp_path, 225000) == pytest.approx(225000 + 1200 - 450.5)  # today's running P&L not included
