"""Performance statistics from an equity curve."""
from __future__ import annotations

import math

import pandas as pd

from backtester.engine import BacktestResult


def summarize(result: BacktestResult) -> dict:
    eq = result.equity
    initial = result.broker.initial_capital
    final = float(eq.iloc[-1])
    drawdown = eq - eq.cummax()
    daily = eq.groupby(eq.index.date).last()
    daily_pnl = daily.diff().fillna(daily.iloc[0] - initial)
    daily_ret = daily_pnl / daily.shift(1).fillna(initial)
    sharpe = (daily_ret.mean() / daily_ret.std() * math.sqrt(252)) if daily_ret.std() > 0 else float("nan")

    return {
        "start": str(eq.index[0]),
        "end": str(eq.index[-1]),
        "initial_capital": initial,
        "final_equity": round(final, 2),
        "net_pnl": round(final - initial, 2),
        "return_pct": round((final / initial - 1) * 100, 2),
        "max_drawdown": round(float(drawdown.min()), 2),
        "max_drawdown_pct": round(float((drawdown / eq.cummax()).min() * 100), 2),
        "sharpe_daily_annualized": round(float(sharpe), 2),
        "trading_days": int(len(daily)),
        "winning_days_pct": round(float((daily_pnl > 0).mean() * 100), 2),
        "best_day": round(float(daily_pnl.max()), 2),
        "worst_day": round(float(daily_pnl.min()), 2),
        "fills": len(result.fills),
        "total_charges": round(result.broker.total_charges, 2),
    }


def fills_frame(result: BacktestResult) -> pd.DataFrame:
    return pd.DataFrame([
        {"ts": f.ts, "symbol": f.instrument.symbol, "side": f.side.value, "qty": f.quantity,
         "price": f.price, "charges": round(f.charges, 2), "tag": f.tag}
        for f in result.fills
    ])
