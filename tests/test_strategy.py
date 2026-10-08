from datetime import datetime
from decimal import Decimal

from config import STRATEGY, TZ
from gex_engine import GexReport
from indicators.supertrend import SuperTrendResult
from strategy import (
    ChainView, build_fly, directional_gate, fly_mid, select_batman_wing, select_single_fly,
)
from tests.fixtures import make_chain

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=TZ)


def trend(bull: bool, mom: float):
    return SuperTrendResult([-1 if bull else 1], [0.0], [0.0], [0.0], [0.0], [mom], 0.05, -0.05)


def report(spot, zero, cw, pw):
    return GexReport(NOW, "2026-10-08", spot, 1.0, "POSITIVE", zero, cw, pw)


def test_vix_regimes():
    assert STRATEGY.regime_for_vix(16.99).name == "ZOMBIELAND"
    assert STRATEGY.regime_for_vix(17).name == "NORMAL"
    assert STRATEGY.regime_for_vix(27.99).name == "NORMAL"
    assert STRATEGY.regime_for_vix(28).name == "CHAOS"
    assert STRATEGY.normal.debit_cap == Decimal("0.30")
    assert STRATEGY.chaos.wing_width == Decimal("4")


def test_directional_conditions():
    r = report(781.2, 774, 784, 775)
    a = directional_gate(r, trend(True, 0.34))
    assert (a.condition, a.option_type, a.target_level) == ("A", "C", 784)
    b = directional_gate(r, trend(False, -0.2))
    assert (b.condition, b.option_type, b.target_level) == ("B", "P", 775)
    r2 = report(770, 774, 784, 765)
    c = directional_gate(r2, trend(False, -0.2))
    assert (c.condition, c.option_type, c.target_level) == ("C", "P", 765)
    dd = directional_gate(r2, trend(True, 0.2))
    assert dd.condition == "D" and dd.option_type is None


def test_momentum_gate_required():
    r = report(781.2, 774, 784, 775)
    out = directional_gate(r, trend(True, 0.01))
    assert out.condition == "STANDBY"


def test_single_fly_shifts_away_to_fit_cap():
    chain = ChainView(make_chain(spot=675.0), "2026-10-08")
    t = 6 / (365 * 24)
    # Fly at 679 prices ~0.53 > 0.30 cap; walking away lands on 682 (~0.24).
    pick = select_single_fly(chain, "C", 679, 675.0, STRATEGY.normal, t, pot_min=0.0)
    assert pick.fly is not None and pick.fly.center == 682
    assert pick.mid <= 0.30 and pick.shifted_by == 3


def test_single_fly_shift_stopped_by_pot_gate():
    chain = ChainView(make_chain(spot=675.0), "2026-10-08")
    # Needs to walk to 682 (long 679) to fit the cap, but PoT there is < 67 %.
    pick = select_single_fly(chain, "C", 679, 675.0, STRATEGY.normal,
                             6 / (365 * 24), pot_min=0.67)
    assert pick.fly is None and "PoT" in pick.reason


def test_single_fly_aborts_when_spot_inside_tent():
    chain = ChainView(make_chain(spot=675.0), "2026-10-08")
    pick = select_single_fly(chain, "C", 677, 675.0, STRATEGY.normal,
                             6 / (365 * 24), pot_min=0.0)
    assert pick.fly is None and "inside" in pick.reason


def test_single_fly_pot_gate_rejects_far_wall():
    rows = make_chain(spot=675.0)
    chain = ChainView(rows, "2026-10-08")
    pick = select_single_fly(chain, "C", 700, 675.0, STRATEGY.normal,
                             6 / (365 * 24), pot_min=0.67)
    assert pick.fly is None and "PoT" in pick.reason


def test_batman_wing_within_window_and_1sd():
    rows = make_chain(spot=675.0, t=25 / (365 * 24), iv=0.14)
    chain = ChainView(rows, "2026-10-09")
    sd = 675 * 0.14 * (25 / (365 * 24)) ** 0.5
    for typ, wall in (("C", 690), ("P", 660)):
        pick = select_batman_wing(chain, typ, wall, 675.0, sd, STRATEGY.zombieland)
        if pick.fly is None:
            continue
        assert 0.15 <= pick.mid <= 0.20
        assert abs(pick.fly.center - 675.0) <= sd
        assert (pick.fly.center > 675) if typ == "C" else (pick.fly.center < 675)


def test_fly_construction_ratios():
    chain = ChainView(make_chain(), "2026-10-08")
    fly = build_fly(chain, "P", 670, 3)
    assert [leg.ratio for leg in fly.legs] == [1, -2, 1]
    assert (fly.lower, fly.center, fly.upper) == (667, 670, 673)
    assert fly_mid(chain, fly) is not None
