from datetime import datetime, timedelta

from config import TZ
from gex_engine import compute_gex, net_gex_by_strike, probability_of_touch
from tests.fixtures import make_chain


def test_walls_and_regime():
    rows = make_chain()
    now = datetime(2026, 10, 8, 9, 0, tzinfo=TZ)
    rep = compute_gex(rows, 675.0, now + timedelta(hours=6), now)
    assert rep.call_wall == 690.0
    assert rep.put_wall == 660.0
    assert rep.zero_gamma is not None
    assert 660 < rep.zero_gamma < 690
    assert rep.one_sd_move and rep.one_sd_move > 0


def test_call_and_put_gex_signs():
    rows = make_chain()
    net = net_gex_by_strike(rows, 675.0)
    assert net[690.0] > 0
    assert net[660.0] < 0


def test_pot_uses_delta_and_handles_touch():
    assert probability_of_touch(675, 676, 0.001, delta=0.40, option_type="C") == 0.80
    assert probability_of_touch(680, 676, 0.001, delta=0.9, option_type="C") == 1.0
