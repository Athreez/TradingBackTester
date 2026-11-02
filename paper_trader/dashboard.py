"""Local dashboard for the paper trading bot. Read-only: it never calls the broker API.

Run:  streamlit run paper_trader/dashboard.py      (opens http://localhost:8501)

Reads paper_trader/state/live.json (written by the bot every poll) and the CSV
journals in paper_trader/logs/.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

HERE = Path(os.environ.get("PAPER_TRADER_DIR", Path(__file__).parent))  # override to inspect a replay
LOG_DIR = HERE / "logs"
STATE_DIR = HERE / "state"
IST = timezone(timedelta(hours=5, minutes=30))
REFRESH = "5s"

# Validated (dataviz validate_palette.js): profit/loss poles + single-series line, light and dark.
PALETTE = {
    "light": {"line": "#2a78d6", "up": "#2a78d6", "down": "#d64545", "grid": "#e1e0d9", "axis": "#c3c2b7", "muted": "#898781"},
    "dark": {"line": "#3987e5", "up": "#3987e5", "down": "#e05c5c", "grid": "#2c2c2a", "axis": "#383835", "muted": "#898781"},
}


# ---- helpers -------------------------------------------------------------

def ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def inr(x, signed: bool = False) -> str:
    """Indian digit grouping: 1234567.8 -> ₹12,34,567.80"""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "–"
    sign = "-" if x < 0 else ("+" if signed and x > 0 else "")
    whole, frac = f"{abs(x):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}₹{','.join(groups + [tail]) if groups else tail}.{frac}"


def bot_running() -> bool:
    """True if the bot holds its single-instance lock."""
    lock_path = STATE_DIR / "bot.lock"
    if not lock_path.exists():
        return False
    try:
        with open(lock_path, "a+") as f:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
        return False  # we could take the lock, so nobody holds it
    except OSError:
        return True


def read_csv(name: str, dates=("ts",)) -> pd.DataFrame:
    path = LOG_DIR / name
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    for col in dates:
        if col in df:
            df[col] = pd.to_datetime(df[col])
    return df


def read_snapshot() -> dict | None:
    path = STATE_DIR / "live.json"
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def theme() -> dict:
    try:
        mode = "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        mode = "light"
    return PALETTE[mode]


def style(chart: alt.Chart, c: dict) -> alt.Chart:
    return chart.configure_axis(
        gridColor=c["grid"], domainColor=c["axis"], tickColor=c["axis"], labelColor=c["muted"],
        titleColor=c["muted"], labelFontSize=11, titleFontSize=11, titleFontWeight="normal",
    ).configure_view(strokeWidth=0)


def line_chart(df: pd.DataFrame, x: str, y: str, x_title: str, x_format: str, c: dict) -> alt.Chart:
    hover = alt.selection_point(fields=[x], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(df).encode(x=alt.X(f"{x}:T", title=x_title, axis=alt.Axis(format=x_format, grid=False)))
    line = base.mark_line(color=c["line"], strokeWidth=2).encode(
        y=alt.Y(f"{y}:Q", title="Equity (₹)", scale=alt.Scale(zero=False)))
    points = base.mark_point(size=80, filled=True, color=c["line"]).encode(
        y=f"{y}:Q", opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip(f"{x}:T", title="Time", format=x_format), alt.Tooltip(f"{y}:Q", title="Equity", format=",.2f")],
    ).add_params(hover)
    rule = base.mark_rule(color=c["axis"]).encode(opacity=alt.condition(hover, alt.value(1), alt.value(0)))
    return style((line + rule + points).properties(height=260), c)


def pnl_bars(df: pd.DataFrame, c: dict) -> alt.Chart:
    df = df.assign(result=lambda d: d["net"].map(lambda v: "Profit" if v >= 0 else "Loss"))
    bars = alt.Chart(df).mark_bar(cornerRadiusEnd=4, size=14).encode(
        x=alt.X("date:T", title="Day", axis=alt.Axis(format="%d %b", grid=False)),
        y=alt.Y("net:Q", title="Net P&L after charges (₹)"),
        color=alt.Color("result:N", scale=alt.Scale(domain=["Profit", "Loss"], range=[c["up"], c["down"]]),
                        legend=alt.Legend(title=None, orient="top-left")),
        tooltip=[alt.Tooltip("date:T", title="Day", format="%a %d %b %Y"),
                 alt.Tooltip("net:Q", title="Net", format=",.2f"),
                 alt.Tooltip("gross:Q", title="Before charges", format=",.2f"),
                 alt.Tooltip("charges:Q", title="Charges", format=",.2f"),
                 alt.Tooltip("fills:Q", title="Fills")],
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=c["axis"]).encode(y="y:Q")
    return style((zero + bars).properties(height=260), c)


# ---- page ----------------------------------------------------------------

st.set_page_config(page_title="Paper Trading Bot", page_icon="📈", layout="wide")


@st.fragment(run_every=REFRESH)
def dashboard() -> None:
    snap = read_snapshot()
    fills = read_csv("fills.csv")
    daily = read_csv("daily.csv", dates=("date",))
    equity = read_csv("equity.csv")
    c = theme()
    now = ist_now()

    title = f"Paper trading · {snap['strategy']}" if snap else "Paper trading"
    st.title(title)
    st.caption(f"Refreshes every {REFRESH} · {now:%a %d %b %Y, %H:%M:%S} IST")

    # -- bot health --------------------------------------------------------
    running = bot_running()
    updated = datetime.fromisoformat(snap["updated_at"]) if snap else None
    age = (now - updated).total_seconds() if updated else None
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Bot", "🟢 Running" if running else "🔴 Stopped")
    h2.metric("Last update", f"{age:.0f}s ago" if age is not None and age < 120 else
              (f"{updated:%d %b %H:%M}" if updated else "never"), help=snap["phase"] if snap else None)
    h3.metric("Last candle", datetime.fromisoformat(snap["last_bar"]).strftime("%d %b %H:%M")
              if snap and snap.get("last_bar") else "–")
    h4.metric("Login token valid until", datetime.fromisoformat(snap["token_valid_until"]).strftime("%d %b %H:%M")
              if snap and snap.get("token_valid_until") else "–")
    if not running:
        st.warning("The bot is not running. Start it with `python -m paper_trader.runner`.", icon="⚠️")
    elif age is not None and age > 60 and snap and snap["phase"] == "trading session":
        st.warning(f"No update for {age:.0f}s during the session. Check the log below.", icon="⚠️")

    if snap is None:
        st.info("No data yet. The dashboard fills in once the bot has started.")
        return

    # -- account -----------------------------------------------------------
    today = datetime.fromisoformat(snap["day"]).date() if snap.get("day") else now.date()  # bot's session day
    today_fills = fills[fills["ts"].dt.date == today] if not fills.empty else fills
    before = daily[daily["date"].dt.date < today] if not daily.empty else daily
    start_equity = float(before["equity"].iloc[-1]) if len(before) else snap["initial_capital"]
    traded = daily[daily["fills"] > 0] if not daily.empty else daily
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Equity", inr(snap["equity"]))
    a2.metric("Total P&L", inr(snap["equity"] - snap["initial_capital"], signed=True),
              help=f"Since start, on {inr(snap['initial_capital'])} capital, after charges")
    a3.metric("Today's P&L", inr(snap["equity"] - start_equity, signed=True),
              help="Equity now (open positions at last price, after charges) minus equity at the previous close")
    a4.metric("Days traded / won", f"{len(traded)} / {int((traded['net'] > 0).sum())}" if len(traded) else "0 / 0")

    # -- today ---------------------------------------------------------------
    st.subheader(f"Session {today:%a %d %b %Y}")
    t1, t2, t3, t4 = st.columns(4)
    t1.metric("Underlying", snap["underlying"] or "no trade scheduled")
    t2.metric("Spot", f"{snap['spot']:,.2f}" if snap.get("spot") else "–")
    t3.metric("India VIX at entry", f"{snap['vix_at_entry']:.2f}" if snap.get("vix_at_entry") else "–")
    entered = (snap.get("note") or "").startswith("entered")
    t4.metric("Stop-loss today", f"{snap['day_sl_pct']:.0%}" if entered and snap.get("day_sl_pct") else "–")
    st.markdown(f"**Status:** {snap.get('note') or '–'}")

    left, right = st.columns(2)
    with left:
        st.markdown("**Open positions**")
        if snap["positions"]:
            pos = pd.DataFrame(snap["positions"]).rename(columns={
                "symbol": "Contract", "qty": "Qty (− = short)", "avg_price": "Entry", "ltp": "Last", "unrealized": "Unrealized P&L"})
            st.dataframe(pos, hide_index=True, width="stretch")
        else:
            st.caption("No open positions.")
    with right:
        st.markdown("**Pending orders (stop-losses)**")
        if snap["open_orders"]:
            orders = pd.DataFrame(snap["open_orders"])[["symbol", "side", "type", "qty", "trigger", "tag"]].rename(columns={
                "symbol": "Contract", "side": "Side", "type": "Type", "qty": "Qty", "trigger": "Trigger", "tag": "Tag"})
            st.dataframe(orders, hide_index=True, width="stretch")
        else:
            st.caption("No pending orders.")

    st.markdown("**Today's fills**")
    if not today_fills.empty:
        st.dataframe(today_fills.assign(ts=today_fills["ts"].dt.strftime("%H:%M")).rename(columns={
            "ts": "Time", "symbol": "Contract", "side": "Side", "qty": "Qty", "price": "Price",
            "charges": "Charges", "tag": "Reason"}), hide_index=True, width="stretch")
    else:
        st.caption("No fills today.")

    if not equity.empty:
        intraday = equity[equity["ts"].dt.date == today]
        if not intraday.empty:
            st.markdown("**Equity today (per minute)**")
            st.altair_chart(line_chart(intraday, "ts", "equity", "Time (IST)", "%H:%M", c), width="stretch")

    # -- history -------------------------------------------------------------
    st.subheader("History")
    if daily.empty:
        st.caption("No completed sessions yet. A row is added at 15:35 each trading day.")
    else:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Net after charges", inr(daily["net"].sum(), signed=True))
        s2.metric("Charges paid", inr(daily["charges"].sum()))
        s3.metric("Win rate (traded days)", f"{(traded['net'] > 0).mean():.0%}" if len(traded) else "–")
        s4.metric("Worst day", inr(daily["net"].min(), signed=True))
        h_left, h_right = st.columns(2)
        with h_left:
            st.markdown("**Daily P&L**")
            st.altair_chart(pnl_bars(daily, c), width="stretch")
        with h_right:
            st.markdown("**Equity at each day's close**")
            st.altair_chart(line_chart(daily, "date", "equity", "Day", "%d %b", c), width="stretch")
        with st.expander("Daily table"):
            st.dataframe(daily.assign(date=daily["date"].dt.strftime("%a %d %b %Y")).rename(columns={
                "date": "Day", "fills": "Fills", "gross": "Before charges", "charges": "Charges", "net": "Net",
                "equity": "Equity"}), hide_index=True, width="stretch")
        with st.expander("All fills"):
            st.dataframe(fills.sort_values("ts", ascending=False), hide_index=True, width="stretch")

    # -- log -------------------------------------------------------------------
    st.subheader("Bot log")
    logs = sorted(LOG_DIR.glob("paper_*.log"))
    if logs:
        lines = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()
        problems = [l for l in lines if " WARNING " in l or " ERROR " in l]
        if problems:
            with st.expander(f"⚠️ {len(problems)} warnings/errors in {logs[-1].name}"):
                st.code("\n".join(problems[-50:]), language=None)
        st.code("\n".join(lines[-25:]) or "(empty)", language=None)
    else:
        st.caption("No log yet.")


dashboard()
