# TradingBackTester

Backtest NIFTY / SENSEX weekly options strategies, then run the same strategy
code in a paper trading bot and later a real Zerodha bot.

```
core/           shared models, Strategy / Broker / MarketData interfaces, cost model
data/           INDmoney API client, downloader CLI, Parquet cache (data/cache, git-ignored)
strategies/     one folder per strategy: strategy.py + config.yaml + README.md
backtester/     event-driven engine, simulated broker, metrics, CLI
paper_trader/   live INDmoney data + simulated broker          (phase 2)
live_trader/    live data + Zerodha Kite orders                (phase 3)
reports/        backtest outputs (git-ignored)
```

| Runner        | Market data          | Broker         |
|---------------|----------------------|----------------|
| backtester    | cached history       | SimBroker      |
| paper_trader  | INDmoney live        | SimBroker      |
| live_trader   | live                 | Zerodha Kite   |

## Setup

```sh
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e ".[dev]"
git config core.hooksPath .githooks   # enables the secret-scanning pre-commit hook
copy .env.example .env            # then fill in INDMONEY_CLIENT_ID / MPIN / TOTP_SECRET
```

## Workflow

```sh
python -m data.downloader token --new    # TOTP login; later runs reuse/refresh it automatically
python -m data.downloader check
python -m data.downloader options --underlying NIFTY --from 2025-09-01 --to 2025-09-30 --interval 1minute
python -m backtester.run --list
python -m backtester.run --strategy triple_iron_fly --from 2025-09-01 --to 2025-09-30
pytest
```

## Adding a strategy

Create `strategies/<name>/strategy.py` with one `Strategy` subclass, plus a
`config.yaml` (`underlyings`, `interval`, `capital`, `costs`, `params`). It is
picked up automatically by the backtester and the bots (`STRATEGY=<name>`).

Backtest semantics: `on_bar` sees the bar that just completed; orders placed
there fill at the next bar's open. Options still open at expiry are settled at
intrinsic value against the index close.

## Secrets

- Credentials live only in `.env` files, which are git-ignored (`.env.example` files hold placeholders).
- The pre-commit hook (`scripts/check_secrets.py`) blocks staged `.env`/key files and token-like values.
- Wrap secrets with `core.config.secret()`; the value is masked in logs and reprs.
- INDmoney: with TOTP configured, tokens are generated automatically (cached in git-ignored
  `.secrets/`, refreshed when expired or rejected). They expire at 7 AM; each new one revokes the
  previous one; 5 bad TOTP codes lock generation for 15 min, so failures are never auto-retried.
- Order placement additionally needs a whitelisted static IP.
