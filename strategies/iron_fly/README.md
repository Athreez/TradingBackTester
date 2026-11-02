# Iron Fly

Intraday iron fly on the nearest weekly expiry. Parameters live in `config.yaml`.

| Day            | Underlying | Hedge distance | Lots |
|----------------|------------|----------------|------|
| Mon, Tue, Fri  | NIFTY      | ATM +/- 400    | 1    |
| Wed, Thu       | SENSEX     | ATM +/- 1000   | 1    |

No trade on the underlying's own expiry day (`skip_expiry_day`), so in practice
NIFTY trades Mon/Fri and SENSEX Wed (plus any day a holiday shifts the expiry).

- **Entry 09:22:** ATM = strike nearest to spot. Buy CE at ATM + hedge and PE at
  ATM - hedge (hedges first), then sell ATM CE and ATM PE. Same lots on all legs.
- **Stop-loss (per side):** each short leg has an SL-M at +50% of its sell price
  (e.g. 100 -> 150). When it fires, that short and its hedge exit; the other side
  keeps running (`on_stop_loss: exit_side`). `exit_all` squares off every leg on
  the first SL instead (tested worse: -54.8k vs -27.2k at 1 lot).
- **Exit 14:45:** square off everything still open.
- One trade per day, no re-entry. Adjustments: none yet.

Backtest notes: SL fires when a 1-minute candle's high touches the trigger and
fills at the trigger (or the open, if it gapped through). Expiry weekdays moved
in Sep 2025 (NIFTY -> Tue, SENSEX -> Thu); the strategy always uses the nearest
expiry, so older backtests see a different days-to-expiry pattern.
