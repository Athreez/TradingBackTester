# Iron Fly

Intraday iron fly on the nearest weekly expiry. Parameters live in `config.yaml`.

| Day          | Underlying | Hedge distance | Lots |
|--------------|------------|----------------|------|
| Mon - Fri    | NIFTY      | ATM +/- 400    | 1    |

SENSEX (Wed/Thu, ATM +/- 1000) is supported but disabled in `config.yaml`.

- **Entry 09:22:** ATM = strike nearest to spot. Buy CE at ATM + hedge and PE at
  ATM - hedge (hedges first), then sell ATM CE and ATM PE. Same lots on all legs.
- **Stop-loss:** each short leg has an SL-M at +50% of its sell price (e.g. 100 -> 150).
  When either one fires, **every remaining leg is squared off** and there is no
  more trading that day (`on_stop_loss: exit_all`). The older per-side behaviour
  (only that side and its hedge exit) is `on_stop_loss: exit_side`.
- **Exit 14:45:** square off everything still open.
- One trade per day, no re-entry. Adjustments: none yet.

Backtest notes: SL fires when a 1-minute candle's high touches the trigger and
fills at the trigger (or the open, if it gapped through). Expiry weekdays moved
in Sep 2025 (NIFTY -> Tue, SENSEX -> Thu); the strategy always uses the nearest
expiry, so older backtests see a different days-to-expiry pattern.
