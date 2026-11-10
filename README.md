# TradingBackTester

Backtest NIFTY / SENSEX weekly options strategies, then run the same strategy
code in a paper trading bot and later a real bot placing orders through INDmoney.

```
core/           shared models, Strategy / Broker / MarketData interfaces, cost model
data/           INDmoney API client, downloader CLI, Parquet cache (data/cache, git-ignored)
strategies/     one folder per strategy: strategy.py + config.yaml + README.md
backtester/     event-driven engine, simulated broker, metrics, CLI
paper_trader/   live INDmoney data + simulated broker          (phase 2)
live_trader/    live data + INDmoney orders                    (phase 3)
reports/        backtest outputs (git-ignored)
```

| Runner        | Market data          | Broker         |
|---------------|----------------------|----------------|
| backtester    | cached history       | SimBroker      |
| paper_trader  | INDmoney live        | SimBroker      |
| live_trader   | INDmoney live        | INDmoney       |

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
python -m backtester.run --strategy vix_iron_fly --from 2025-09-01 --to 2025-09-30
python -m backtester.sweep --strategy vix_iron_fly --from 2025-09-01 --to 2026-10-30 --param stop_loss_pct=30,40,50
pytest
```

## Paper trading

```sh
copy paper_trader\.env.example paper_trader\.env   # STRATEGY=vix_iron_fly, CAPITAL=500000
start_paper_trading.bat                # double-click: bot + dashboard in two windows
python -m paper_trader.runner          # runs every trading day until Ctrl+C
python -m paper_trader.runner --once   # just today's session
```

The bot polls INDmoney 1-minute candles and runs the same engine step as the
backtester (`backtester.engine.run_bar`): orders fill at the next candle's open,
SL-M stops trigger on candle highs. Output goes to `paper_trader/logs/`
(`fills.csv`, `daily.csv`, `equity.csv`, a log per day). State is saved after
every bar, so a restart resumes open positions and stop-losses instead of
re-entering; only one bot can run at a time.

### Live order mirroring (empty account)

With `LIVE_ORDERS=mirror` in `paper_trader/.env`, the paper bot also sends each
order it places to INDmoney for real (market orders; stop-losses as GTT smart
orders). The account is meant to hold no money, so every order is rejected for
margin. A rejection has no fill price, so for each order `live_trader/mirror.py`
records the live 5-level order book at the moment of sending and what it would
have filled our quantity at (`live_trader/logs/orders.csv`), every status
INDmoney reports, with rejection reasons and GTT trigger times (`events.csv`),
and the paper fill vs that book price (`compare.csv`).

Safeguards:
- Nothing is sent if INDmoney reports more than `LIVE_MAX_BALANCE` (default Rs 100) available.
- Orders from bars replayed after a late start are never sent, and there is a daily cap.
- Open orders are cancelled at day end and on restart.
- An order that fills anyway is flattened at once, and mirroring stops for the day.

Needs the PC's static IP registered at indstocks.com/app/api-trading/access-tokens.

### Simulating past sessions

```sh
python -m paper_trader.simulate --from 2026-10-01 --to 2026-11-03
```

Replays the downloaded candles minute by minute through the paper bot's engine
and writes the same journals to `paper_trader/sim/` (the live account is never
touched). Pick **Simulation** in the dashboard sidebar to view it.

### Checking the data

```sh
python -m data.audit --from 2025-09-01 --to 2026-11-03
```

Index coverage per session, option naming vs strike/type/expiry, expiry-day
consistency and put-call parity at entry. Run it after every download.

### Dashboard

```sh
pip install -e ".[dashboard]"
streamlit run paper_trader/dashboard.py     # or scripts
un_dashboard.cmd -> http://localhost:8501
```

Read-only view of the bot (never calls the broker API): bot health and token
expiry, today's VIX / stop-loss rule / legs with live P&L and pending stops,
fills, intraday equity, daily P&L history and the log tail. Refreshes every 5s.

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
