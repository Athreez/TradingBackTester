"""Local dashboard for the trading bots. Read-only: it never calls the broker API.

Run:  streamlit run paper_trader/dashboard.py      (opens http://localhost:8501)

A toggle at the top right switches between the paper bot (paper_trader/) and the live bot (live_trader/);
the sidebar shows a simulation instead, when one exists
(paper_trader/sim/). Each view reads that bot's state/live.json (written every poll) and the CSV
journals in its logs/ folder.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

HERE = Path(os.environ.get("PAPER_TRADER_DIR", Path(__file__).parent))  # override to inspect a replay
LIVE_DIR = Path(os.environ.get("LIVE_TRADER_DIR", Path(__file__).parent.parent / "live_trader"))
LOCK_PATH = HERE / "state" / "bot.lock"  # the paper and live bots share this lock (one INDmoney login)
LOG_DIR = HERE / "logs"
STATE_DIR = HERE / "state"
IST = timezone(timedelta(hours=5, minutes=30))
WATCH_EVERY = "1s"  # how often the status row ticks and checks for new bot data

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
    lock_path = LOCK_PATH
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


def wrote_last(snap: dict | None) -> bool:
    """The lock says *a* bot runs (paper and live share it); it is this view's bot if this view's snapshot
    is newer than the other bot's."""
    other = HERE / "state" / "live.json" if LIVE else LIVE_DIR / "state" / "live.json"
    try:
        theirs = json.loads(other.read_text()).get("updated_at", "")
    except (OSError, ValueError):
        theirs = ""
    return bool(snap) and (snap.get("updated_at") or "") >= theirs


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
    for _ in range(5):
        try:
            return json.loads(path.read_text())
        except PermissionError:  # Windows: the bot is swapping the file in right now
            time.sleep(0.05)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
    return None


def data_signature() -> tuple:
    """Changes only when the bot writes new data, not on its every-poll heartbeat (updated_at)."""
    snap = read_snapshot()
    if snap:
        snap.pop("updated_at", None)
    files = [LOG_DIR / n for n in ("fills.csv", "daily.csv", "equity.csv")] + sorted(LOG_DIR.glob("*_????????.log"))[-1:]
    return json.dumps(snap, sort_keys=True), tuple((p.name, p.stat().st_mtime_ns) for p in files if p.exists())


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


def line_chart(df: pd.DataFrame, x: str, y: str, x_title: str, x_format: str, c: dict,
               y_title: str = "Equity (₹)", label: str = "Equity", zero_line: bool = False) -> alt.Chart:
    hover = alt.selection_point(fields=[x], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(df).encode(x=alt.X(f"{x}:T", title=x_title, axis=alt.Axis(format=x_format, grid=False)))
    line = base.mark_line(color=c["line"], strokeWidth=2).encode(
        y=alt.Y(f"{y}:Q", title=y_title, scale=alt.Scale(zero=False)))
    points = base.mark_point(size=80, filled=True, color=c["line"]).encode(
        y=f"{y}:Q", opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip(f"{x}:T", title="Time", format=x_format), alt.Tooltip(f"{y}:Q", title=label, format="+,.2f" if zero_line else ",.2f")],
    ).add_params(hover)
    rule = base.mark_rule(color=c["axis"]).encode(opacity=alt.condition(hover, alt.value(1), alt.value(0)))
    layers = [line, rule, points]
    if zero_line:  # P&L charts: mark break-even
        layers.insert(0, alt.Chart(pd.DataFrame({"zero": [0]})).mark_rule(color=c["axis"], strokeDash=[4, 4]).encode(y="zero:Q"))
    return style(alt.layer(*layers).properties(height=260), c)


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


def closed_positions(fills: pd.DataFrame) -> pd.DataFrame:
    """Contracts opened and fully closed within these fills, with realized P&L (before and after charges)."""
    rows = []
    for symbol, f in fills.sort_values("ts").groupby("symbol", sort=False):
        signed = f["qty"].where(f["side"] == "BUY", -f["qty"])
        if signed.sum() != 0:
            continue  # still open
        opening = f["side"] == f["side"].iloc[0]
        value = f["qty"] * f["price"]
        realized = value.where(f["side"] == "SELL", -value).sum()
        rows.append({
            "Contract": symbol, "Qty (− = short)": int(signed[opening].sum()),
            "Entry": value[opening].sum() / f.loc[opening, "qty"].sum(),
            "Exit": value[~opening].sum() / f.loc[~opening, "qty"].sum(),
            "Closed at": f.loc[~opening, "ts"].max().strftime("%H:%M"),
            "Realized P&L": realized, "Charges": f["charges"].sum(), "Net P&L": realized - f["charges"].sum(),
        })
    return pd.DataFrame(rows)


def with_total(df: pd.DataFrame, sum_cols: list[str]):
    """df plus a bold Total row summing sum_cols; other cells of that row are left blank."""
    total = {col: df[col].sum() if col in sum_cols else None for col in df.columns}
    total[df.columns[0]] = "Total"
    out = pd.concat([df, pd.DataFrame([total])], ignore_index=True)
    for col in out.columns:  # keep whole-number columns whole despite the blank total cell
        if col not in sum_cols and pd.api.types.is_integer_dtype(df[col]):
            out[col] = out[col].astype("Int64")
    floats = [col for col in out.columns if pd.api.types.is_float_dtype(out[col])]
    bold = ["font-weight: bold"] * len(out.columns)
    return (out.style.format("{:,.2f}", subset=floats, na_rep="").format(na_rep="")
            .apply(lambda r: bold if r.name == len(out) - 1 else [""] * len(r), axis=1))


# ---- page ----------------------------------------------------------------

st.set_page_config(page_title="Trading Bot", page_icon="📈", layout="wide")

# Paper account, live (real money) account, or a replay written by `python -m paper_trader.simulate`.
SIM_DIR = HERE / "sim"

# Paper / Live switch, top right under Streamlit's Deploy button, with the current mode shown above it.
# Kept in the URL (?view=live) so a refresh or a bookmark opens the same view.
_, switch = st.columns([10, 1])
with switch:
    mode_label = st.empty()
    LIVE = st.toggle("Live", value=st.query_params.get("view") == "live", key="live_view",
                     help="Off = the paper bot (simulated fills). On = the real-money bot (INDmoney orders).")
    mode_label.markdown(f"<div style='text-align:center;font-weight:600;white-space:nowrap'>"
                        f"{'💰 Live' if LIVE else '📝 Paper'}</div>", unsafe_allow_html=True)
st.query_params["view"] = "live" if LIVE else "paper"
# Past sessions replayed with `python -m paper_trader.simulate --from ... --to ...` (only when one exists).
SIMULATION = (SIM_DIR / "state" / "live.json").exists() and st.sidebar.toggle(
    "🧪 Show simulation", help="Past sessions replayed through the bot's engine, instead of the bot above.")
if SIMULATION:
    LIVE = False
if SIMULATION:
    LOG_DIR, STATE_DIR = SIM_DIR / "logs", SIM_DIR / "state"
elif LIVE:
    LOG_DIR, STATE_DIR = LIVE_DIR / "logs", LIVE_DIR / "state"


@st.fragment(run_every=WATCH_EVERY)
def live_status() -> None:
    """Ticks every second; reruns the whole page only when the bot's data has changed (like a React re-render)."""
    sig = data_signature()
    if st.session_state.setdefault("data_sig", sig) != sig:
        st.session_state.data_sig = sig
        st.rerun()

    snap = read_snapshot()
    now = ist_now()
    if SIMULATION:
        sim = (snap or {}).get("simulation", {})
        st.info(f"Simulation: sessions {sim.get('from', '?')} to {sim.get('to', '?')} replayed minute by minute "
                "through the paper bot's engine on downloaded candles. Not the live account.", icon="🧪")
        return
    st.caption(f"{'💰 LIVE bot (real money)' if LIVE else 'Paper bot'} · redraws when the bot writes new data · "
               f"{now:%a %d %b %Y, %H:%M:%S} IST")

    # -- bot health --------------------------------------------------------
    running = bot_running() and wrote_last(snap)
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
        st.warning(f"The {'live' if LIVE else 'paper'} bot is not running. Start it with "
                   + ("`start_live_trading.bat`." if LIVE else "`start_paper_trading.bat`."), icon="⚠️")
    elif snap and snap["phase"].startswith(("login problem", "loading contracts failed")):
        st.error(snap["phase"], icon="🚫")
    elif age is not None and age > 60 and snap and snap["phase"] == "trading session":
        st.warning(f"No update for {age:.0f}s during the session. Check the log below.", icon="⚠️")


def dashboard() -> None:
    snap = read_snapshot()
    fills = read_csv("fills.csv")
    daily = read_csv("daily.csv", dates=("date",))
    equity = read_csv("equity.csv")
    c = theme()
    now = ist_now()

    kind = "Simulation" if SIMULATION else "💰 Live trading" if LIVE else "Paper trading"
    st.title(f"{kind} · {snap['strategy']}" if snap else kind)
    live_status()

    if snap is None:
        st.info(f"No data yet. This fills in once the {'live' if LIVE else 'paper'} bot has run.")
        return
    marker = STATE_DIR / "simulated_history.json"
    if not SIMULATION and marker.exists():
        sim = json.loads(marker.read_text())
        st.info(f"Sessions {sim['from']} to {sim['to']} were simulated (replayed minute by minute through the "
                f"bot's engine on recorded candles). Everything after {sim['to']} is live paper trading.", icon="🧪")

    # -- account -----------------------------------------------------------
    today = datetime.fromisoformat(snap["day"]).date() if snap.get("day") else now.date()  # bot's session day
    today_fills = fills[fills["ts"].dt.date == today] if not fills.empty else fills
    before = daily[daily["date"].dt.date < today] if not daily.empty else daily
    start_equity = float(before["equity"].iloc[-1]) if len(before) else snap["initial_capital"]
    traded = daily[daily["fills"] > 0] if not daily.empty else daily
    fig = snap.get("indmoney") if LIVE else None  # live: INDmoney's own figures (live_trader/figures.py)
    ind_daily = read_csv("indmoney_daily.csv", dates=("date",)) if LIVE else pd.DataFrame()
    if not ind_daily.empty:
        ind_daily = ind_daily.drop_duplicates("date", keep="last").sort_values("date")
    a1, a2, a3, a4 = st.columns(4)
    if LIVE:
        recorded = set(ind_daily["date"].dt.date) if not ind_daily.empty else set()
        total = (ind_daily["net"].sum() if not ind_daily.empty else 0.0) + (
            fig["net"] if fig and today not in recorded else 0.0)
        ind_traded = ind_daily[ind_daily["traded"] > 0] if not ind_daily.empty else ind_daily
        start = float(snap.get("start_capital") or snap["initial_capital"])
        a1.metric("Capital", inr(start + (ind_daily["net"].sum() if not ind_daily.empty else 0.0)),
                  help=f"{inr(start)} start (CAPITAL in .env) plus INDmoney's net P&L of each closed day. "
                       "Updated once a day, after the close; not INDmoney's margin figure.")
        a2.metric("Total P&L", inr(total, signed=True), help="INDmoney: sum of each day's net P&L since the live bot started")
        a3.metric("Today's P&L", inr(fig["net"], signed=True) if fig else "–",
                  help=(f"INDmoney: realized {inr(fig['realized'], True)} + unrealized {inr(fig['unrealized'], True)} "
                        f"− brokerage {inr(fig['brokerage'])} − charges {inr(fig['charges'])}") if fig else None)
        a4.metric("Days traded / won", f"{len(ind_traded)} / {int((ind_traded['net'] > 0).sum())}" if len(ind_traded) else "0 / 0")
        st.caption(f"All figures from INDmoney, as of {fig['at'][11:19]}" if fig else
                   "INDmoney's figures appear once the live bot has read them (every 30 s while running).")
    else:
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
    if snap.get("halted"):
        st.error(f"No new entries: {snap['halted']}", icon="⛔")

    left, right = st.columns(2)
    with left:
        st.markdown("**Open positions**")
        open_ind = [p for p in fig["positions"] if p["qty"]] if fig else []
        if LIVE:
            if open_ind:
                st.dataframe(pd.DataFrame(open_ind)[["contract", "qty", "avg_price"]].rename(columns={
                    "contract": "Contract", "qty": "Qty (− = short)", "avg_price": "Avg price"}), hide_index=True, width="stretch")
                st.caption(f"Unrealized P&L (INDmoney): {inr(fig['unrealized'], signed=True)}")
            else:
                st.caption("No open positions on INDmoney.")
        elif snap["positions"]:
            pos = pd.DataFrame(snap["positions"]).rename(columns={
                "symbol": "Contract", "qty": "Qty (− = short)", "avg_price": "Entry", "ltp": "Last", "unrealized": "Unrealized P&L"})
            st.dataframe(with_total(pos, ["Unrealized P&L"]), hide_index=True, width="stretch")
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
    if snap.get("live_orders"):
        with st.expander(f"INDmoney orders today ({len(snap['live_orders'])})"):
            st.dataframe(pd.DataFrame(snap["live_orders"]).rename(columns={
                "tag": "Tag", "symbol": "Contract", "side": "Side", "qty": "Qty", "kind": "Kind", "filled": "Filled",
                "price": "Avg price", "done": "Done", "status": "INDmoney status"}), hide_index=True, width="stretch")

    st.markdown("**Closed positions today**")
    closed = closed_positions(today_fills) if not today_fills.empty and not LIVE else pd.DataFrame()
    closed_ind = [p for p in fig["positions"] if not p["qty"]] if fig else []
    if LIVE:
        if closed_ind:
            st.dataframe(with_total(pd.DataFrame(closed_ind)[["contract", "buy_qty", "buy_avg", "sell_qty", "sell_avg", "realized"]]
                                    .rename(columns={"contract": "Contract", "buy_qty": "Bought", "buy_avg": "Buy avg",
                                                     "sell_qty": "Sold", "sell_avg": "Sell avg", "realized": "Realized P&L"}),
                                    ["Realized P&L"]), hide_index=True, width="stretch")
            st.caption(f"INDmoney realized P&L, before brokerage {inr(fig['brokerage'])} and charges {inr(fig['charges'])}.")
        else:
            st.caption("No closed positions today on INDmoney.")
    elif not closed.empty:
        st.dataframe(with_total(closed, ["Realized P&L", "Charges", "Net P&L"]), hide_index=True, width="stretch")
    else:
        st.caption("No closed positions today.")

    st.markdown("**Today's fills**")
    if LIVE and not today_fills.empty:  # INDmoney's fill prices; the bot's charge estimate is left out
        today_fills = today_fills.drop(columns=["charges"], errors="ignore")
    if not today_fills.empty:
        st.dataframe(today_fills.assign(ts=today_fills["ts"].dt.strftime("%H:%M")).rename(columns={
            "ts": "Time", "symbol": "Contract", "side": "Side", "qty": "Qty", "price": "Price",
            "charges": "Charges", "tag": "Reason"}), hide_index=True, width="stretch")
    else:
        st.caption("No fills today.")

    if LIVE:
        ind_intraday = read_csv("indmoney_intraday.csv")
        ind_intraday = ind_intraday[ind_intraday["ts"].dt.date == today] if not ind_intraday.empty else ind_intraday
        if not ind_intraday.empty:
            st.markdown("**P&L today (INDmoney, every 30 s)**")
            st.altair_chart(line_chart(ind_intraday, "ts", "net", "Time (IST)", "%H:%M", c,
                                       y_title="Today's P&L (₹)", label="P&L", zero_line=True), width="stretch")
    elif not equity.empty:
        intraday = equity[equity["ts"].dt.date == today]
        if not intraday.empty:
            st.markdown("**P&L today (per minute)**")
            st.altair_chart(line_chart(intraday.assign(pnl=intraday["equity"] - start_equity), "ts", "pnl", "Time (IST)",
                                       "%H:%M", c, y_title="Today's P&L (₹)", label="P&L", zero_line=True),
                            width="stretch")

    # -- history -------------------------------------------------------------
    st.subheader("History")
    if LIVE:
        if ind_daily.empty:
            st.caption("No completed live sessions yet. INDmoney's figures are recorded at each day's close.")
        else:
            d = ind_daily.assign(charges=ind_daily["brokerage"] + ind_daily["charges"],
                                 capital=float(snap.get("start_capital") or snap["initial_capital"]) + ind_daily["net"].cumsum())
            dt = d[d["traded"] > 0]
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Net (INDmoney)", inr(d["net"].sum(), signed=True))
            s2.metric("Brokerage + charges (INDmoney)", inr(d["charges"].sum()))
            s3.metric("Win rate (traded days)", f"{(dt['net'] > 0).mean():.0%}" if len(dt) else "–")
            s4.metric("Worst day", inr(d["net"].min(), signed=True))
            h_left, h_right = st.columns(2)
            with h_left:
                st.markdown("**Daily P&L (INDmoney)**")
                st.altair_chart(pnl_bars(d, c), width="stretch")
            with h_right:
                st.markdown("**Capital at each close**")
                st.altair_chart(line_chart(d, "date", "capital", "Day", "%d %b", c, y_title="Capital (₹)",
                                           label="Capital"), width="stretch")
            with st.expander("Daily table (INDmoney)"):
                st.dataframe(d.assign(date=d["date"].dt.strftime("%a %d %b %Y"))[
                    ["date", "traded", "realized", "unrealized", "brokerage", "charges", "net", "capital"]].rename(columns={
                    "date": "Day", "traded": "Traded", "realized": "Realized", "unrealized": "Unrealized",
                    "brokerage": "Brokerage", "charges": "Brokerage + charges", "net": "Net", "capital": "Capital"}),
                    hide_index=True, width="stretch")
            with st.expander("All fills (INDmoney prices)"):
                st.dataframe(fills.drop(columns=["charges"], errors="ignore").sort_values("ts", ascending=False)
                             if not fills.empty else fills, hide_index=True, width="stretch")
    elif daily.empty:
        st.caption("No completed sessions yet. A row is added at 15:35 each trading day.")
    else:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Net after charges", inr(daily["net"].sum(), signed=True))
        s2.metric("Charges paid", inr(daily["charges"].sum()),
                  help="INDmoney charges: Rs 10 brokerage per order, plus STT, exchange, SEBI, stamp duty and GST")
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
    logs = sorted(LOG_DIR.glob("*_????????.log"))
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
