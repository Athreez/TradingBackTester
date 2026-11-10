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

Backtest, 2 Apr 2025 - 9 Nov 2026 (INDmoney data), 1 lot, GTT stop-limits,
INDmoney charges: net +1,33,584 (+26.7%), max drawdown -21,518, 212 days traded,
66% winners (Apr - Aug 2025: 53 days, +52,381; Sep 2025 onwards: 159 days,
+81,203). Charges 26,401 over 1,696 orders. VIX thresholds and stop levels were
chosen on the Sep 2025 onwards data, so only Apr - Aug 2025 is out-of-sample.

Charges model: core/costs.py reproduces INDmoney's own /margin itemisation for
API option orders (exchange 0.03503% and IPFT 0.0005% on NSE and BSE alike, GST
not on IPFT; tested against 8 of its totals), and charges older trades the STT
and exchange rates of their date (STT on sold options 0.05% until Mar 2023,
0.0625% until Sep 2024, 0.1% until Mar 2026, 0.15% since).

### Five years (Hugging Face data)

INDmoney has no expired contracts before Apr 2025. `python -m data.import_hf`
builds a store from the codepyx23/india-index-options-1m dataset (NIFTY weeklies
from 2021, SENSEX weeklies from Aug 2023) for `backtester.run --data-root
data/cache/hf_store`. India VIX there is 1-minute from 21 May 2024 and the day's
opening VIX before that (same VIX regime as 09:22 on 91% of days). Where both
datasets overlap (Apr 2025 - Jul 2026) they agree on 98.8% of traded days' sign.

2 Aug 2021 - 2 Jul 2026, 1 lot (lot sizes of each period):

| Year | Net    | `max_days_to_expiry: 1` |
|------|--------|-------------------------|
| 2021 (Aug-Dec) | +14,865 | +17,323 |
| 2022 | -1,588 | +28,343 |
| 2023 | -14,412 | -10,315 |
| 2024 | +37,489 | +40,683 |
| 2025 | +1,02,926 | +1,00,126 |
| 2026 (to Jul) | +25,022 | +30,334 |
| **Total** | **+1,64,302**, max DD -25,547, 747 days | **+2,06,494**, max DD -17,352, 422 days |

Almost all the profit is on expiry day and the day before; 2+ days out lost money
in both 2021-2023 and 2024-2026, and was most of what was traded before SENSEX
weeklies existed (Aug 2023). On the INDmoney period `max_days_to_expiry: 1` gives
+1,36,846 (max DD -11,432, 169 days) vs +1,33,584. That rule was found on this
same data, so it is in-sample; it is off by default.

GTT stop-limits vs SL-M over the same period: of 233 stops, 2 filled a minute
later than SL-M would have; net -532. A 2% buffer costs -2,948, a 10% buffer
-1,103.

Backtest notes: a stop triggers when a 1-minute candle's high touches the trigger
and fills at the trigger (or the open, if it gapped through) when that is within
the limit; otherwise it rests at the limit until the chase replaces it. Expiry weekdays moved
in Sep 2025 (NIFTY Thu -> Tue, SENSEX Tue -> Thu).
