# VIX Iron Fly

Intraday iron fly on the nearest weekly expiry, gated by India VIX. Parameters
live in `config.yaml`.

| Day            | Underlying | Hedge distance | Lots |
|----------------|------------|----------------|------|
| Mon, Tue, Fri  | NIFTY      | ATM +/- 400    | 1    |
| Wed, Thu       | SENSEX     | ATM +/- 1000   | 1    |

Expiry days (NIFTY Tue, SENSEX Thu) are traded.

| India VIX at entry | Action                     |
|--------------------|----------------------------|
| below 11           | trade, stop-loss 40%       |
| 11 to 14           | no trade                   |
| above 14           | trade, stop-loss 30%       |

- **Entry 09:22:** ATM = strike nearest to spot. Buy CE at ATM + hedge and PE at
  ATM - hedge (hedges first), then sell ATM CE and ATM PE. Same lots on all legs.
- **Stop-loss (per side):** each short leg has an SL-M at +30% / +40% (by VIX) of
  its sell price. When it fires, that short and its hedge exit; the other side
  keeps running.
- **Exit 14:45:** square off everything still open.
- One trade per day, no re-entry.

Backtest, Sep 2025 - Oct 2026, 1 lot, Zerodha charges: net +54,719 (+10.9%),
max drawdown -21,899, 151 trading days, 66% winners. VIX thresholds and stop
levels were chosen on that same data, so treat it as in-sample.

Backtest notes: SL fires when a 1-minute candle's high touches the trigger and
fills at the trigger (or the open, if it gapped through). Expiry weekdays moved
in Sep 2025 (NIFTY -> Tue, SENSEX -> Thu).
