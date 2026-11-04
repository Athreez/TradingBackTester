# VIX Iron Fly

Intraday iron fly on the nearest weekly expiry, gated by India VIX. Parameters
live in `config.yaml`.

Each day trades whichever underlying's weekly expiry comes soonest, so the
schedule follows the exchanges' expiry weekdays:

| Expiry days                         | NIFTY days    | SENSEX days   |
|-------------------------------------|---------------|---------------|
| since Sep 2025: NIFTY Tue, SENSEX Thu | Mon, Tue, Fri | Wed, Thu      |
| until Aug 2025: NIFTY Thu, SENSEX Tue | Wed, Thu      | Mon, Tue, Fri |

| Underlying | Hedge distance | Lots |
|------------|----------------|------|
| NIFTY      | ATM +/- 400    | 1    |
| SENSEX     | ATM +/- 1300   | 1    |

Expiry days are traded. Lot sizes come from `data/underlyings.py` by expiry date.

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

Backtest, 2 Apr 2025 - 3 Nov 2026, 1 lot, Zerodha charges: net +95,263
(+19.1%), max drawdown -22,938, 208 days traded, 65% winners (Apr - Aug 2025:
53 days, +42,449; Sep 2025 onwards: 155 days, +52,814). VIX thresholds and stop
levels were chosen on the Sep 2025 onwards data, so only Apr - Aug 2025 is
out-of-sample. INDmoney has no expired contracts before Apr 2025.

Backtest notes: SL fires when a 1-minute candle's high touches the trigger and
fills at the trigger (or the open, if it gapped through). Expiry weekdays moved
in Sep 2025 (NIFTY Thu -> Tue, SENSEX Tue -> Thu).
