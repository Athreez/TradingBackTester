from datetime import date

import pandas as pd

from backtester.run import run_backtest
from paper_trader.simulate import simulate
from test_vix_iron_fly import BASE, store  # noqa: F401 (pytest fixture)


def test_simulation_matches_backtest_and_writes_bot_journals(store, tmp_path):  # noqa: F811
    day = date(2025, 9, 8)
    result = simulate("vix_iron_fly", day, day, capital=500000, out=tmp_path, store=store, overrides=BASE)
    backtest = run_backtest("vix_iron_fly", day, day, store=store, capital=500000, overrides=BASE)

    fills = pd.read_csv(tmp_path / "logs" / "fills.csv")
    assert len(fills) == len(backtest.fills) == 8
    assert fills["price"].tolist() == [round(f.price, 2) for f in backtest.fills]
    assert result["net"] == round(backtest.equity.iloc[-1] - 500000, 2)
    daily = pd.read_csv(tmp_path / "logs" / "daily.csv")
    assert daily["date"].tolist() == ["2025-09-08"]
    assert (tmp_path / "state" / "live.json").exists()
    assert (tmp_path / "logs" / "paper_20250908.log").read_text(encoding="utf-8").startswith("2025-09-08 09:05")
