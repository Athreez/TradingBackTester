"""INDmoney's own account figures, as the live bot records them for the dashboard and Telegram.

The live bot reads GET /funds (realized / unrealized P&L, brokerage, F&O charges, available balance) and
GET /portfolio/positions every 30 s, keeps the latest in its snapshot (`indmoney`), appends the day's
running P&L to logs/indmoney_intraday.csv and, at the close, one row per day to logs/indmoney_daily.csv.
Nothing here is the bot's own bookkeeping: these are INDmoney's numbers.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

DAILY = "indmoney_daily.csv"
INTRADAY = "indmoney_intraday.csv"


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def position_name(row: dict) -> str:
    """INDmoney position row -> 'NIFTY 25050 CE 17 Nov'."""
    name = row.get("symbol") or str(row.get("security_id") or "?")
    if row.get("drv_option_type"):
        strike = _f(row.get("drv_strike_price"))
        name += f" {strike:g} {row['drv_option_type']}"
        try:
            name += f" {datetime.strptime(str(row.get('drv_expiry_date'))[:10], '%m/%d/%Y'):%d %b}"
        except ValueError:
            pass
    return name


def figures(funds: dict, positions: list[dict], at: datetime) -> dict:
    """Today's figures from INDmoney's /funds and /portfolio/positions responses."""
    realized, unrealized = _f(funds.get("realized_pnl")), _f(funds.get("unrealized_pnl"))
    brokerage = _f(funds.get("brokerage"))
    charges = _f(funds.get("fno_charges")) + _f(funds.get("eq_charges"))
    avl = funds.get("detailed_avl_balance") or {}
    return {
        "at": at.isoformat(timespec="seconds"),
        "available": _f(avl.get("option_sell")) if avl else _f(funds.get("withdrawal_balance")),
        "sod_balance": _f(funds.get("sod_balance")),
        "funds_added": _f(funds.get("funds_added")),
        "funds_withdrawn": _f(funds.get("funds_withdrawn")),
        "realized": realized, "unrealized": unrealized, "brokerage": brokerage, "charges": charges,
        "net": realized + unrealized - brokerage - charges,
        "positions": [{"contract": position_name(p), "qty": int(_f(p.get("net_qty"))), "avg_price": _f(p.get("avg_price")),
                       "buy_qty": int(_f(p.get("buy_qty"))), "buy_avg": _f(p.get("buy_avg")),
                       "sell_qty": int(_f(p.get("sell_qty"))), "sell_avg": _f(p.get("sell_avg")),
                       "realized": _f(p.get("realized_profit"))} for p in positions],
    }


def append(log_dir: Path, name: str, row: dict) -> None:
    path = log_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def daily_row(day: date, fig: dict, traded: bool) -> dict:
    return {"date": day.isoformat(), "traded": int(traded), "realized": round(fig["realized"], 2),
            "unrealized": round(fig["unrealized"], 2), "brokerage": round(fig["brokerage"], 2),
            "charges": round(fig["charges"], 2), "net": round(fig["net"], 2), "available": round(fig["available"], 2)}


def read_daily(log_dir: Path) -> list[dict]:
    """One row per day (the last one written wins), oldest first."""
    path = log_dir / DAILY
    if not path.exists():
        return []
    by_day = {r["date"]: r for r in csv.DictReader(path.open())}
    return [by_day[d] for d in sorted(by_day)]


def capital(log_dir: Path, start: float) -> float:
    """The live account's capital: CAPITAL from .env (e.g. 2,25,000) plus INDmoney's net P&L of every closed
    day. It changes once a day, after the close; INDmoney's margin figures (which pledges and blocked
    amounts move around) are not used for it."""
    return start + sum(_f(r["net"]) for r in read_daily(log_dir))


def total_since_start(log_dir: Path, fig: dict | None, today: date) -> float:
    """Sum of INDmoney's daily net P&L, plus today's running figure if today isn't recorded yet."""
    rows = read_daily(log_dir)
    total = sum(_f(r["net"]) for r in rows)
    if fig and today.isoformat() not in {r["date"] for r in rows}:
        total += fig["net"]
    return total
