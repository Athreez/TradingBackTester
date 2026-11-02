from datetime import date, datetime

import pandas as pd
import pytest

from backtester.engine import BacktestEngine
from backtester.historical_data import HistoricalMarketData
from backtester.metrics import summarize
from backtester.sim_broker import SimBroker
from core.costs import CostModel
from core.models import OptionType
from core.strategy_base import Strategy
from data.store import DataStore

EXPIRY = date(2025, 1, 2)
SYMBOL = "NIFTY25JAN0224000CE"


def _candles(start: str, closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq="1min", name="ts")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                         "volume": 1.0}, index=idx)


@pytest.fixture
def store(tmp_path):
    s = DataStore(tmp_path)
    # Day 1 and expiry day, 3 bars each. Index ends expiry at 24050 -> 24000 CE worth 50.
    s.save_candles("1minute", "NSE", "NIFTY", pd.concat([
        _candles("2025-01-01 09:15", [24100, 24120, 24110]),
        _candles("2025-01-02 09:15", [24080, 24060, 24050]),
    ]))
    s.save_candles("1minute", "NFO", SYMBOL, pd.concat([
        _candles("2025-01-01 09:15", [150, 140, 145]),
        _candles("2025-01-02 09:15", [90, 70, 55]),
    ]))
    s.save_contracts("NIFTY", EXPIRY, pd.DataFrame([{
        "trading_symbol": SYMBOL, "instrument_type": "OPTIDX", "strike_price": 24000.0,
        "option_type": "CE", "lot_size": 75, "expiry": EXPIRY.isoformat(),
    }]))
    return s


class SellOneCall(Strategy):
    def on_start(self, ctx):
        self.done = False
        self.seen = []

    def on_bar(self, ctx):
        self.seen.append((ctx.now, ctx.data.spot("NIFTY")))
        if not self.done:
            expiry = ctx.data.expiries("NIFTY")[0]
            inst = ctx.data.find_option("NIFTY", expiry, 24000.0, OptionType.CE)
            ctx.sell(inst, lots=1, tag="short-ce")
            self.done = True


def test_short_call_held_to_expiry(store):
    data = HistoricalMarketData(store, "1minute")
    timeline = [t.to_pydatetime() for t in data.index_frame("NIFTY").index]
    no_costs = CostModel(brokerage_per_order=0, stt_sell=0, stt_exercise=0, sebi_fee=0, stamp_duty_buy=0,
                         gst=0, exchange_txn={}, ipft={})
    broker = SimBroker(lambda i: data.bar_at(i, data.now()), no_costs, 100_000)
    strategy = SellOneCall({})

    result = BacktestEngine(strategy, data, broker, timeline).run()

    # Order placed on bar 1 fills at bar 2's open (140), not bar 1 (no look-ahead).
    entry, settle = result.fills
    assert entry.price == 140 and entry.ts == datetime(2025, 1, 1, 9, 16)
    # Settled at intrinsic 24050 - 24000 = 50 at end of expiry day.
    assert settle.tag == "expiry-settlement" and settle.price == 50
    assert result.equity.iloc[-1] == pytest.approx(100_000 + 75 * (140 - 50))
    assert broker.positions() == {}
    # Strategy only ever saw the current bar's close.
    assert strategy.seen[0] == (datetime(2025, 1, 1, 9, 15), 24100)

    summary = summarize(result)
    assert summary["net_pnl"] == pytest.approx(75 * 90)
    assert summary["trading_days"] == 2


def test_limit_order_waits_for_price(store):
    data = HistoricalMarketData(store, "1minute")
    broker = SimBroker(lambda i: data.bar_at(i, data.now()), CostModel(), 100_000)
    data.set_time(datetime(2025, 1, 1, 9, 15))
    inst = data.option_chain("NIFTY", EXPIRY)[0]
    from core.strategy_base import Context
    Context(data, broker).buy(inst, 1, limit=142)

    data.set_time(datetime(2025, 1, 1, 9, 16))  # bar at 140 -> fills at 140
    fills = broker.process(data.now())
    assert len(fills) == 1 and fills[0].price == 140
