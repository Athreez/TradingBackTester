"""Paper trading bot: live INDmoney market data + SimBroker. (Phase 2)

Usage: python -m paper_trader.runner
"""
from __future__ import annotations

from pathlib import Path

from core.config import env, load_env
from core.registry import load_strategy


def main() -> None:
    load_env(Path(__file__).parent / ".env")
    name = env("STRATEGY")
    if not name:
        raise SystemExit("Set STRATEGY in paper_trader/.env (see paper_trader/.env.example)")
    strategy, config = load_strategy(name)
    print(f"Loaded strategy '{name}' on {config.get('underlyings')} ({config.get('interval')})")
    # TODO phase 2: LiveMarketData (INDmoney quotes/websocket) + SimBroker with live LTP quotes,
    # driving the same Context / Strategy lifecycle the backtester uses.
    raise NotImplementedError("Paper trading loop is built in phase 2")


if __name__ == "__main__":
    main()
