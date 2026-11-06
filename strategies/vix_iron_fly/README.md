# VIX Iron Fly

Intraday iron fly on the nearest weekly expiry, gated by India VIX. Parameters
live in `config.yaml`.

Each day trades whichever underlying's weekly expiry comes soonest, so the
schedule follows the exchanges' expiry weekdays:

| Expiry days                         | NIFTY days    | SENSEX days   |
|-------------------------------------|---------------|---------------|
| since Sep 2025: NIFTY Tue, SENSEX Thu | Mon, Tue, Fri | Wed, Thu      |
| until Aug 2025: NIFTY Thu, SENSEX Tue | Wed, Thu      | Mon, Tue, Fri |

| Underlying | Hedge distance                          | Lots |
|------------|-----------------------------------------|------|
| NIFTY      | ATM +/- 2% of spot, nearest 50 points   | 1    |
| SENSEX     | ATM +/- 2% of spot, nearest 100 points  | 1    |

A fixed `hedge_distance` (points) can be set instead of `hedge_pct`.

If a hedge strike is illiquid at entry (any of the 5 one-minute bars before 09:22
untraded, or under 500 lots traded in total), the nearest round strike is used
instead (NIFTY multiples of 100, SENSEX of 500; ties go nearer ATM). If that is
illiquid too, the day is skipped. In the Apr 2025 - Nov 2026 data the thinnest
2% hedge still traded ~4,700 lots in those 5 minutes, so the fallback never fired.

Expiry days are traded. Lot sizes come from `data/underlyings.py` by expiry date.

| India VIX at entry | Action                     |
|--------------------|----------------------------|
| below 11           | trade, stop-loss 40%       |
| 11 to 14           | no trade                   |
| above 14           | trade, stop-loss 30%       |

- **Entry 09:22:** ATM = strike nearest to spot. Buy CE at ATM + hedge and PE at
  ATM - hedge (hedges first), then sell ATM CE and ATM PE. Same lots on all legs.
- **Stop-loss (per side):** each short leg has a stop at +30% / +40% (by VIX) of
  its sell price, placed as an INDmoney GTT-style stop-limit: limit 5% above the
  trigger, prices on the 0.05 tick. If price gaps past the limit and the stop is
  still unfilled a minute after triggering, it is replaced by a market buy. When
  it fills, that short and its hedge exit; the other side keeps running.
  (`stop_order.type: market` switches back to SL-M.)
- **Exit 14:45:** square off everything still open.
- One trade per day, no re-entry.

Backtest, 2 Apr 2025 - 3 Nov 2026, 1 lot, GTT stop-limits, INDmoney charges:
net +1,22,312 (+24.5%), max drawdown -21,512, 208 days traded, 66% winners
(Apr - Aug 2025: 53 days, +51,986; Sep 2025 onwards: 155 days, +70,326).
Charges 26,739 over 1,664 orders; at Zerodha's Rs 20 brokerage the net would be
19,635 lower. VIX thresholds and stop
levels were chosen on the Sep 2025 onwards data, so only Apr - Aug 2025 is
out-of-sample. INDmoney has no expired contracts before Apr 2025.

GTT stop-limits vs SL-M over the same period: of 233 stops, 2 filled a minute
later than SL-M would have; net -532. A 2% buffer costs -2,948, a 10% buffer
-1,103.

Backtest notes: a stop triggers when a 1-minute candle's high touches the trigger
and fills at the trigger (or the open, if it gapped through) when that is within
the limit; otherwise it rests at the limit until the chase replaces it. Expiry weekdays moved
in Sep 2025 (NIFTY Thu -> Tue, SENSEX Tue -> Thu).
