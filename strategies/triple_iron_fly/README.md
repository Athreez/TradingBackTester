# Triple Iron Fly

Intraday iron fly on the nearest weekly expiry. Parameters live in `config.yaml`.

| Day            | Underlying | Hedge distance |
|----------------|------------|----------------|
| Mon, Tue, Fri  | NIFTY      | ATM +/- 400    |
| Wed, Thu       | SENSEX     | ATM +/- 1000   |

- **Entry 09:22:** ATM = strike nearest to spot. Buy CE at ATM + hedge and PE at
  ATM - hedge (hedges first), then sell ATM CE and ATM PE. Same lots on all legs.
- **Stop-loss (per side):** if a short leg's premium rises 50% above its sell
  price (e.g. 100 -> 150), buy it back (SL-M at the trigger) and sell that side's
  hedge. The other side keeps running.
- **Exit 14:45:** square off everything still open.
- One trade per day, no re-entry. Adjustments: none yet.

Backtest notes: SL fires when a 1-minute candle's high touches the trigger and
fills at the trigger (or the open, if it gapped through). Expiry weekdays moved
in Sep 2025 (NIFTY -> Tue, SENSEX -> Thu); the strategy always uses the nearest
expiry, so older backtests see a different days-to-expiry pattern.
