"""Real trading bot placing orders through the INDmoney (INDstocks) API. (Phase 3)

Usage: python -m live_trader.runner
"""
from __future__ import annotations

from pathlib import Path

from core.config import env, load_env


def main() -> None:
    load_env(Path(__file__).parent / ".env")
    if env("LIVE_TRADING_ENABLED") != "yes":
        raise SystemExit("LIVE_TRADING_ENABLED is not 'yes' in live_trader/.env -- refusing to start.")
    # TODO phase 3: IndMoneyBroker(Broker) (orders + GTT smart orders, static IP whitelisted) + the paper
    # bot's live MarketData, same Strategy lifecycle as backtests.
    raise NotImplementedError("Live trading is built in phase 3")


if __name__ == "__main__":
    main()
