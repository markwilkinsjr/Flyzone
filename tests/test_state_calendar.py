from datetime import date

import market_calendar as cal
from models import Fly, FlyLeg, TradeSet, Wing
from state import BotState, StateStore


def test_state_roundtrip(tmp_path):
    legs = [FlyLeg("a", "A", 781, "C", 1), FlyLeg("b", "B", 784, "C", -2),
            FlyLeg("c", "C", 787, "C", 1)]
    ts = TradeSet("SINGLE_FLY", "NORMAL", "2026-10-08", "2026-10-08",
                  [Wing(Fly("C", 781, 784, 787, "2026-10-08", legs), status="FILLED",
                        entry_debit="0.30", floor_armed=True, floor="0.60")],
                  "2026-10-08T10:00:00-05:00", "A")
    store = StateStore(tmp_path / "s.json")
    store.save(BotState(day="2026-10-08", vix=18.4, tradeset=ts, paused=True))
    back = store.load()
    assert back.tradeset.wings[0].floor_armed
    assert back.tradeset.wings[0].fly.legs[1].ratio == -2
    assert back.paused and back.vix == 18.4


def test_calendar():
    assert not cal.is_trading_day(date(2026, 12, 25))
    assert cal.is_early_close(date(2026, 11, 27))       # day after Thanksgiving
    assert cal.is_early_close(date(2026, 12, 24))
    assert cal.next_trading_day(date(2026, 10, 9)) == date(2026, 10, 12)   # Fri -> Mon
    assert cal.expiration_for_dte(date(2026, 10, 8), 1) == date(2026, 10, 9)
