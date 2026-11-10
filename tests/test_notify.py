from datetime import date, datetime
from types import SimpleNamespace

from core.models import Exchange, Fill, Instrument, Side
from paper_trader.notify import TradeNotifier, inr, short_name

CE = Instrument("NIFTY26NOV1724550CE", Exchange.NFO, "NIFTY", lot_size=65)
HEDGE = Instrument("NIFTY26NOV1725050CE", Exchange.NFO, "NIFTY", lot_size=65)
T = lambda hm: datetime.fromisoformat(f"2026-11-11 {hm}")


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def test_entry_stop_and_summary_messages():
    tg = FakeTelegram()
    n = TradeNotifier(tg)
    strategy = SimpleNamespace(today=("NIFTY", {}), day_vix=15.2, day_sl_pct=0.3, note="entered; CE stop-loss hit at 09:40")
    entry = [Fill(1, HEDGE, Side.BUY, 65, 19.2, T("09:22"), tag="hedge:CE"),
             Fill(2, CE, Side.SELL, 65, 142.5, T("09:22"), tag="short:CE")]
    n.fills(entry, entry, strategy)
    assert "ENTRY NIFTY" in tg.sent[0] and "VIX 15.20 · stop-loss 30%" in tg.sent[0]
    assert "SELL NIFTY 24550 CE × 65 @ 142.50" in tg.sent[0] and "Net credit Rs 8,014" in tg.sent[0]

    sl = Fill(3, CE, Side.BUY, 65, 185.3, T("09:40"), tag="sl:CE")
    n.fills([sl], entry + [sl], strategy)
    assert "STOP-LOSS" in tg.sent[1] and "entry 142.50, -Rs 2,782" in tg.sent[1]

    n.day_summary(date(2026, 11, 11), {"fills": 8, "gross": 3718.8, "charges": 126.0, "net": 3592.8,
                                       "equity": 554130.4}, strategy, 500_000, "INDmoney: 8 FAILED")
    s = tg.sent[2]
    assert "Net <b>+Rs 3,593</b>" in s and "since start +Rs 54,130" in s and "INDmoney: 8 FAILED" in s

    n.day_summary(date(2026, 11, 12), {"fills": 0, "equity": 554130.4}, SimpleNamespace(note="VIX 12.40 is in the no-trade band, no trade"), 500_000)
    assert "no trade" in tg.sent[3] and "no-trade band" in tg.sent[3]


def test_helpers():
    assert short_name("SENSEX26NOV1277400PE") == "SENSEX 77400 PE"
    assert inr(-2782.4, True) == "-Rs 2,782" and inr(1500) == "Rs 1,500"
