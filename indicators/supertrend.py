"""
Python port of "SWMA Momentum SuperTrend | RakoQuant" (Pine Script v6).

The reference source is ``rq_swma_momentum_supertrend.pine`` in this folder.
The port is a literal bar-by-bar translation so the bot's signal matches the
TradingView chart. Pine semantics that matter and are reproduced here:

* ``na`` is represented by ``float('nan')``. Any comparison with NaN is
  ``False`` in Python, which is exactly how Pine treats ``na`` comparisons.
* ``nz(x)`` returns 0 when ``x`` is ``na``.
* ``ta.ema`` / ``ta.rma`` seed with a simple moving average of the first
  ``length`` values, then recurse.
* ``ta.swma`` is the fixed 4-bar symmetric weighted MA (1/6, 2/6, 2/6, 1/6).
* ``ta.atr`` uses ``high - low`` for the very first bar (no previous close).
* ``upperband[1]`` refers to the *final* value of the previous bar, i.e.
  after the band-locking reassignment with ``:=``.

Only closed bars should be fed in (Pine's ``barstate.isconfirmed``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

NaN = float("nan")


def _na(x: float) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _nz(x: float) -> float:
    return 0.0 if _na(x) else x


def _sma_window(values: Sequence[float], end: int, length: int) -> float:
    """Pine ta.sma at bar ``end``: NaN if not enough bars or any NaN in the window."""
    if end + 1 < length:
        return NaN
    window = values[end + 1 - length:end + 1]
    if any(_na(v) for v in window):
        return NaN
    return sum(window) / length


def pine_ema(values: Sequence[float], length: int) -> list[float]:
    """ta.ema: alpha = 2/(len+1), seeded by SMA while the previous value is na."""
    alpha = 2.0 / (length + 1)
    out: list[float] = []
    for i, v in enumerate(values):
        prev = out[i - 1] if i > 0 else NaN
        if _na(prev):
            out.append(_sma_window(values, i, length))
        else:
            out.append(alpha * v + (1 - alpha) * _nz(prev) if not _na(v) else NaN)
    return out


def pine_rma(values: Sequence[float], length: int) -> list[float]:
    """ta.rma (Wilder's smoothing): alpha = 1/len, seeded by SMA."""
    alpha = 1.0 / length
    out: list[float] = []
    for i, v in enumerate(values):
        prev = out[i - 1] if i > 0 else NaN
        if _na(prev):
            out.append(_sma_window(values, i, length))
        else:
            out.append(alpha * v + (1 - alpha) * _nz(prev) if not _na(v) else NaN)
    return out


def pine_swma(values: Sequence[float]) -> list[float]:
    """ta.swma: x[3]*1/6 + x[2]*2/6 + x[1]*2/6 + x*1/6."""
    out: list[float] = []
    for i in range(len(values)):
        if i < 3:
            out.append(NaN)
            continue
        out.append(
            values[i - 3] / 6 + values[i - 2] * 2 / 6
            + values[i - 1] * 2 / 6 + values[i] / 6
        )
    return out


def pine_atr(high: Sequence[float], low: Sequence[float],
             close: Sequence[float], length: int) -> list[float]:
    tr: list[float] = []
    for i in range(len(high)):
        if i == 0:
            tr.append(high[i] - low[i])
        else:
            tr.append(max(high[i] - low[i],
                          abs(high[i] - close[i - 1]),
                          abs(low[i] - close[i - 1])))
    return pine_rma(tr, length)


@dataclass(frozen=True)
class SuperTrendParams:
    swma_smooth: int = 8
    atr_len: int = 10
    factor: float = 2.2
    mom_len: int = 7
    use_smooth: bool = True
    smooth_len: int = 3
    mom_gate: float = 0.5
    intraday_mode: bool = True   # smoothing only applies in Intraday mode


@dataclass
class SuperTrendResult:
    direction: list[int]       # -1 bullish, +1 bearish (Pine convention)
    st: list[float]            # active SuperTrend line
    upper: list[float]
    lower: list[float]
    baseline: list[float]
    mom_norm: list[float]
    gate_up: float
    gate_down: float

    # Convenience accessors for the latest closed bar ----------------------
    @property
    def last_direction(self) -> int:
        return self.direction[-1]

    @property
    def is_bullish(self) -> bool:
        return self.direction[-1] < 0

    @property
    def last_mom_norm(self) -> float:
        return self.mom_norm[-1]

    @property
    def momentum_gate_active(self) -> bool:
        """True when normalized momentum clears the gate in the trend's direction."""
        m = self.mom_norm[-1]
        return m > self.gate_up if self.is_bullish else m < self.gate_down

    @property
    def last_band(self) -> float:
        return self.st[-1]


def swma_momentum_supertrend(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    params: SuperTrendParams = SuperTrendParams(),
    src: Sequence[float] | None = None,
) -> SuperTrendResult:
    """Run the indicator over full bar history. ``src`` defaults to ``high``."""
    n = len(close)
    if not (len(high) == len(low) == n):
        raise ValueError("high/low/close must be the same length")
    src = list(high if src is None else src)

    swma_raw = pine_swma(src)
    baseline = pine_ema(swma_raw, params.swma_smooth)
    atr = pine_atr(high, low, close, params.atr_len)
    mom = [
        baseline[i] - baseline[i - params.mom_len] if i >= params.mom_len else NaN
        for i in range(n)
    ]
    # NaN arithmetic propagates naturally (nan - x = nan), matching Pine.

    smooth = params.use_smooth and params.intraday_mode
    baseline_s = pine_ema(baseline, params.smooth_len) if smooth else baseline
    atr_s = pine_ema(atr, params.smooth_len) if smooth else atr
    mom_s = pine_ema(mom, params.smooth_len) if smooth else mom

    gate_up = (1.0 - params.mom_gate) * 0.0 + params.mom_gate * 0.10
    gate_down = -(1.0 - params.mom_gate) * 0.0 - params.mom_gate * 0.10

    upper: list[float] = []
    lower: list[float] = []
    direction: list[int] = []
    st: list[float] = []
    mom_norm: list[float] = []

    for i in range(n):
        ub = baseline_s[i] + params.factor * atr_s[i]
        lb = baseline_s[i] - params.factor * atr_s[i]

        prev_upper = _nz(upper[i - 1]) if i > 0 else 0.0
        prev_lower = _nz(lower[i - 1]) if i > 0 else 0.0
        prev_close = close[i - 1] if i > 0 else NaN

        # Band locking: the upper band may only move down (and the lower band
        # only up) unless the previous close broke through it.
        ub = ub if (ub < prev_upper or prev_close > prev_upper) else prev_upper
        lb = lb if (lb > prev_lower or prev_close < prev_lower) else prev_lower
        upper.append(ub)
        lower.append(lb)

        a = atr_s[i]
        # Pine: `atr_s != 0.0` is false when atr_s is na, so momNorm = 0.0
        mn = (mom_s[i] / a) if (not _na(a) and a != 0.0) else 0.0
        mom_norm.append(mn)

        prev_st = _nz(st[i - 1]) if i > 0 else 0.0
        prev_ub_final = _nz(upper[i - 1]) if i > 0 else 0.0

        if i == 0 or _na(baseline_s[i - 1]) or _na(atr_s[i - 1]):
            d = 1
        else:
            was_bear = prev_st == prev_ub_final
            flip_bull = close[i] > ub and mn > gate_up
            flip_bear = close[i] < lb and mn < gate_down
            if was_bear:
                d = -1 if flip_bull else 1
            else:
                d = 1 if flip_bear else -1
        direction.append(d)
        st.append(lb if d == -1 else ub)

    return SuperTrendResult(
        direction=direction, st=st, upper=upper, lower=lower,
        baseline=baseline_s, mom_norm=mom_norm,
        gate_up=gate_up, gate_down=gate_down,
    )


def min_bars_required(params: SuperTrendParams = SuperTrendParams()) -> int:
    """Rough warm-up length before the output is trustworthy."""
    warm = 3 + params.swma_smooth + params.mom_len + params.smooth_len
    return max(warm, params.atr_len + params.smooth_len) * 3
