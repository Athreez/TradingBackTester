from datetime import date, datetime

import paper_trader.runner as runner
from paper_trader.simulate import simulate
from test_vix_iron_fly import BASE, store  # noqa: F401 (pytest fixture)


def test_live_bot_resumes_from_simulated_history(store, tmp_path, monkeypatch):  # noqa: F811
    day = date(2025, 9, 8)
    result = simulate("vix_iron_fly", day, day, capital=500000, out=tmp_path, store=store, overrides=BASE,
                      wipe=False, write_state=True)
    assert (tmp_path / "state" / "simulated_history.json").exists()

    # Start the live bot against that folder the next morning.
    monkeypatch.setattr(runner, "STATE_PATH", tmp_path / "state" / "state.pkl")
    monkeypatch.setattr(runner, "ist_now", lambda: datetime(2025, 9, 9, 8, 0))
    monkeypatch.setattr(runner.IndMoneyClient, "from_env", classmethod(lambda cls, **k: None))
    bot = runner.PaperBot("vix_iron_fly", 500000)

    assert abs(bot.broker.cash - (500000 + result["net"])) < 0.01
    assert len(bot.broker.fills()) == 8 and bot.broker.positions() == {}
    assert bot.day == day and bot.day_closed
    assert bot.strategy.params["vix_regimes"]  # a new day starts from config, not the pickled strategy
