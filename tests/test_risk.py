from datetime import datetime
from decimal import Decimal

from config import TZ
from models import Fly, FlyLeg, FlyQuote, Wing
from risk_manager import evaluate_wing, realized_pnl, value_fly

EXP = "2026-10-08"


def wing(entry="0.30", center=784.0, w=3.0):
    legs = [FlyLeg("a", "A", center - w, "C", 1), FlyLeg("b", "B", center, "C", -2),
            FlyLeg("c", "C", center + w, "C", 1)]
    return Wing(fly=Fly("C", center - w, center, center + w, EXP, legs),
                status="FILLED", entry_debit=entry, high_water=entry)


def q(v):
    return FlyQuote(v, v, v)


def t(h, m, day=8):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def test_floor_arms_at_100pct_and_never_drops():
    w = wing()
    r = evaluate_wing(w, 783.0, q(0.60), t(11, 0))
    assert "FLOOR_ARMED" in r.events and w.floor_armed and Decimal(w.floor) == Decimal("0.60")
    evaluate_wing(w, 783.0, q(0.90), t(11, 5))
    assert Decimal(w.floor) == Decimal("0.60")   # fixed at 2x before 2:15


def test_hold_inside_tent_even_below_floor():
    w = wing()
    evaluate_wing(w, 783.0, q(0.70), t(11, 0))
    r = evaluate_wing(w, 783.0, q(0.50), t(11, 5))   # in tent, below floor
    assert not r.exit


def test_floor_exit_outside_tent():
    w = wing()
    evaluate_wing(w, 783.0, q(0.70), t(11, 0))
    r = evaluate_wing(w, 780.5, q(0.55), t(11, 5))   # left tent, below floor
    assert r.exit and r.reason == "PROFIT_FLOOR"


def test_users_trail_example_after_215():
    w = wing()
    evaluate_wing(w, 784.0, q(0.90), t(14, 16))
    assert w.trail_active and Decimal(w.floor) == Decimal("0.72")
    evaluate_wing(w, 784.0, q(1.10), t(14, 20))
    assert Decimal(w.floor) == Decimal("0.88")
    r = evaluate_wing(w, 787.5, q(0.88), t(14, 25))    # outside tent, touches floor
    assert r.exit and r.reason == "TRAILING_LOCK"


def test_trail_not_active_before_expiration_day():
    w = wing()
    evaluate_wing(w, 784.0, q(0.90), t(14, 30, day=7))  # day before expiry (1DTE Batman)
    assert not w.trail_active


def test_left_tent_winner_exits_loser_held():
    w = wing()
    r = evaluate_wing(w, 788.0, q(0.40), t(12, 0))
    assert r.exit and r.reason == "LEFT_TENT"
    loser = wing()
    r2 = evaluate_wing(loser, 788.0, q(0.10), t(12, 0))
    assert not r2.exit


def test_value_fly_mid_and_natural():
    w = wing()
    quotes = {"A": (1.00, 1.10), "B": (0.40, 0.45), "C": (0.05, 0.10)}
    v = value_fly(w.fly, quotes)
    assert abs(v.mid - (1.05 - 0.85 + 0.075)) < 1e-9
    assert abs(v.natural_sell - (1.00 - 0.90 + 0.05)) < 1e-9


def test_realized_pnl():
    pnl, roi = realized_pnl(Decimal("0.30"), Decimal("0.72"), 1)
    assert pnl == Decimal("42.00") and roi == Decimal("1.4")
