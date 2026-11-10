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
    # TODO phase 3: IndMoneyBroker(Broker) driven by real fills, on top of the order calls in
    # data/indmoney_client.py. Until then, live_trader/mirror.py (LIVE_ORDERS=mirror in paper_trader/.env)
    # sends the paper bot's orders to an empty INDmoney account to measure real-market behaviour.
    raise NotImplementedError("Live trading is built in phase 3")


if __name__ == "__main__":
    main()
