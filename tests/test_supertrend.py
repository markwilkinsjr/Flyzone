import math

from indicators.supertrend import (
    SuperTrendParams, pine_atr, pine_ema, pine_rma, pine_swma, swma_momentum_supertrend,
)


def test_swma_weights():
    out = pine_swma([1, 2, 3, 4, 5])
    assert all(math.isnan(x) for x in out[:3])
    assert out[3] == (1 + 2 * 2 + 3 * 2 + 4) / 6


def test_ema_seeded_by_sma():
    vals = [float("nan"), 1, 2, 3, 4, 5]
    out = pine_ema(vals, 3)
    assert math.isnan(out[2])            # window still contains nan
    assert out[3] == 2.0                 # SMA(1,2,3) seeds the EMA
    assert out[4] == 0.5 * 4 + 0.5 * 2.0


def test_rma_and_atr_first_bar_uses_high_minus_low():
    atr = pine_atr([10, 11, 12], [9, 10, 11], [9.5, 10.5, 11.5], 2)
    assert math.isnan(atr[0])
    # TR = [1, max(1, 1.5, .5)=1.5, 1.5]; RMA(2): seed avg(1,1.5)=1.25
    assert atr[1] == 1.25
    assert abs(atr[2] - (0.5 * 1.5 + 0.5 * 1.25)) < 1e-12
    assert pine_rma([2, 4, 6], 2)[1] == 3


def _series(trend, n=200, start=670.0):
    closes = [start + trend * i + 0.3 * math.sin(i / 3) for i in range(n)]
    highs = [c + 0.4 for c in closes]
    lows = [c - 0.4 for c in closes]
    return highs, lows, closes


def test_uptrend_turns_bullish_with_gate():
    h, lo, c = _series(+0.25)
    r = swma_momentum_supertrend(h, lo, c, SuperTrendParams())
    assert r.is_bullish
    assert r.momentum_gate_active
    assert r.last_band < c[-1]          # bullish line sits below price


def test_downtrend_is_bearish():
    h, lo, c = _series(-0.25)
    r = swma_momentum_supertrend(h, lo, c, SuperTrendParams())
    assert not r.is_bullish
    assert r.momentum_gate_active
    assert r.last_band > c[-1]


def test_band_locking_upper_never_rises_in_bear_state():
    h, lo, c = _series(-0.25)
    r = swma_momentum_supertrend(h, lo, c, SuperTrendParams())
    tail = r.upper[60:]
    for prev, cur, pc in zip(tail, tail[1:], c[60:]):
        assert cur <= prev or pc > prev
