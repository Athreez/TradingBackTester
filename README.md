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

### Telegram notifications

Set `TELEGRAM_BOT_TOKEN` (from @BotFather) and `TELEGRAM_CHAT_IDS=[id1, id2]` in
`.env` and the bot messages each entry, stop-loss and exit with its
P&L, INDmoney's answer to each mirrored order, safety alerts, and a day-end
summary to every chat in that whitelist. `python -m paper_trader.notify chat-id`
finds chat ids after you message the bot once (only while the bot is stopped);
while it runs, a message from a chat not on the list is ignored and its id is
logged once. `python -m paper_trader.notify test` sends a test message.

The running bot also answers commands from whitelisted chats only, each reply
going to the chat that asked (`/help` lists them):
`/status`, `/today`, `/pnl`, `/history 10`, `/live`, `/health`, `/log 20`, and
the switches `/pause` / `/resume` (skip new entries), `/exitnow` (close
everything, after a YES), `/mirror on|off` and `/mute` / `/unmute` (alerts always
come through). Switches are kept in `paper_trader/state/controls.json`. Only one
program can read the bot's messages, so `notify chat-id` gets nothing while the
bot is running.

### Live order mirroring (empty account)

With `LIVE_ORDERS=mirror` in `.env`, the paper bot also sends each
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

### Live trading (real money)

```sh
start_live_trading.bat                 # or: python -m live_trader.runner [--once]
```

The paper bot's loop with a real broker (`live_trader/broker.py`): same strategy, candles, Telegram
messages and commands, but orders go to INDmoney and fills are read back from it. Needs
`LIVE_TRADING_ENABLED=yes` in `.env` and the static IP above. It takes the paper bot's lock, so only one
of the two runs (they would share one INDmoney login). Journals in `live_trader/logs/` (`orders.csv` and
`events.csv` record every order sent and every status INDmoney reported), state in `live_trader/state/`.

How orders are handled:
- Hedges go first; the shorts are sent only once both hedges have filled. If any entry leg fails, nothing
  more is sent, whatever filled is closed, and the day is over.
- Stop-losses are GTT trigger orders, held on INDmoney's servers (they fire even if the PC is off). A stop
  INDmoney refuses is replaced by closing that leg at market.
- INDmoney turns market orders into limit orders within a price band; one not filled within a minute is
  cancelled and the rest sent again (3 tries, then an alert to do it by hand).
- An exit waits until INDmoney confirms the cancel of that leg's stop, and is capped to what is still
  open, so a stop that fires during the exit is never bought back twice.
- A request with no clear answer (timeout) is looked up by its `remarks` before anything is resent.
- Every 30 s the positions INDmoney reports (intraday product) are checked against the bot's. A stop that
  shows only in positions is booked at the day's average price; any other difference that lasts 90 s
  stops new entries and alerts.
- Refused outright: orders above `LIVE_MAX_LOTS`, entries from bars replayed after a late start, entries
  past `LIVE_MAX_ORDERS` a day. At day end every open order, GTTs included, is cancelled.

No webhooks: INDmoney has no order webhook, and the bot polls `/order-book` every 5 s instead (its order
WebSocket would be faster but needs a second always-open connection).

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

- Credentials and settings live only in the root `.env`, which is git-ignored (`.env.example` holds placeholders).
- The pre-commit hook (`scripts/check_secrets.py`) blocks staged `.env`/key files and token-like values.
- Wrap secrets with `core.config.secret()`; the value is masked in logs and reprs.
- INDmoney: with TOTP configured, tokens are generated automatically (cached in git-ignored
  `.secrets/`, refreshed when expired or rejected). They expire at 7 AM; each new one revokes the
  previous one; 5 bad TOTP codes lock generation for 15 min, so failures are never auto-retried.
- Order placement additionally needs a whitelisted static IP.
