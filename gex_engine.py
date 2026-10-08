"""
Gamma Exposure (GEX) engine for a single XSP expiration.

Inputs are plain ``OptionRow`` records (strike, type, open interest, gamma,
IV, delta, bid, ask) so this module has no network dependency and can be unit
tested with fixtures. ``tasty_client.MarketData`` builds the rows from the
live production feed.

Formulas (per the agreed spec):

    Call GEX(K) =  Gamma * OI * Spot * 100
    Put  GEX(K) = -Gamma * OI * Spot * 100
    Net  GEX(K) = Call GEX(K) + Put GEX(K)

Structural levels:

* Call Wall  - strike with the largest positive net GEX.
* Put Wall   - strike with the largest negative net GEX.
* Zero Gamma - the spot price at which total GEX changes sign. Gamma is
  re-computed with Black-Scholes at hypothetical spot prices around the
  current spot (each option keeps its own IV), and the crossing nearest to
  the current spot is linearly interpolated. If no crossing exists inside the
  sweep, it falls back to the strike where the cumulative net-GEX profile
  changes sign.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

import numpy as np
from scipy.stats import norm

CONTRACT_MULTIPLIER = 100
MIN_T_YEARS = 1.0 / (365 * 24 * 60)   # one minute; avoids divide-by-zero at expiry


@dataclass(frozen=True)
class OptionRow:
    strike: float
    option_type: str          # "C" or "P"
    open_interest: float
    gamma: float | None
    iv: float | None          # annualised, decimal (0.18 = 18 %)
    delta: float | None = None
    bid: float | None = None
    ask: float | None = None
    symbol: str = ""          # OCC symbol, used for order legs
    streamer_symbol: str = ""

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


@dataclass
class GexReport:
    timestamp: datetime
    expiration: str
    spot: float
    total_gex: float
    regime: str                         # "POSITIVE" or "NEGATIVE"
    zero_gamma: float | None
    call_wall: float | None
    put_wall: float | None
    net_by_strike: dict[float, float] = field(default_factory=dict)
    atm_iv: float | None = None
    one_sd_move: float | None = None    # expected 1-SD move to expiration (points)

    @property
    def above_zero_gamma(self) -> bool | None:
        if self.zero_gamma is None:
            return None
        return self.spot > self.zero_gamma


# --------------------------------------------------------------------------- #
# Black-Scholes helpers
# --------------------------------------------------------------------------- #
def bs_gamma(spot: float, strike: float, iv: float, t_years: float,
             rate: float = 0.0) -> float:
    if spot <= 0 or strike <= 0 or iv <= 0:
        return 0.0
    t = max(t_years, MIN_T_YEARS)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    return float(norm.pdf(d1) / (spot * iv * math.sqrt(t)))


def prob_itm(spot: float, strike: float, iv: float, t_years: float,
             option_type: str) -> float:
    """Risk-neutral probability of finishing beyond the strike (N(d2))."""
    t = max(t_years, MIN_T_YEARS)
    d2 = (math.log(spot / strike) - 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    return float(norm.cdf(d2) if option_type == "C" else norm.cdf(-d2))


def probability_of_touch(spot: float, strike: float, t_years: float,
                         iv: float | None = None, delta: float | None = None,
                         option_type: str = "C") -> float:
    """
    Probability that price touches ``strike`` before expiration.

    Standard trader's approximation: PoT ~= 2 x probability ITM ~= 2 x |delta|.
    The feed's delta is preferred; Black-Scholes N(d2) is the fallback.
    """
    if option_type == "C" and spot >= strike:
        return 1.0
    if option_type == "P" and spot <= strike:
        return 1.0
    if delta is not None and not math.isnan(delta) and delta != 0:
        return min(1.0, 2.0 * abs(delta))
    if iv is None or iv <= 0:
        return 0.0
    return min(1.0, 2.0 * prob_itm(spot, strike, iv, t_years, option_type))


def years_until(expiry: datetime, now: datetime) -> float:
    return max((expiry - now).total_seconds(), 0.0) / (365.0 * 24 * 3600)


# --------------------------------------------------------------------------- #
# Core calculations
# --------------------------------------------------------------------------- #
def net_gex_by_strike(rows: Iterable[OptionRow], spot: float) -> dict[float, float]:
    """Call GEX positive, put GEX negative, summed per strike."""
    out: dict[float, float] = {}
    for r in rows:
        if r.gamma is None or math.isnan(r.gamma) or r.open_interest <= 0:
            continue
        sign = 1.0 if r.option_type == "C" else -1.0
        gex = sign * r.gamma * r.open_interest * spot * CONTRACT_MULTIPLIER
        out[r.strike] = out.get(r.strike, 0.0) + gex
    return dict(sorted(out.items()))


def total_gex_at(rows: Sequence[OptionRow], spot: float | np.ndarray,
                 t_years: float) -> np.ndarray | float:
    """
    Total GEX if the index were at ``spot`` (gamma re-computed with each
    option's own IV held fixed). Vectorised over an array of spot levels.
    """
    usable = [r for r in rows if r.iv and r.iv > 0 and r.open_interest > 0]
    s = np.atleast_1d(np.asarray(spot, dtype=float))
    if not usable:
        out = np.zeros_like(s)
        return out if np.ndim(spot) else float(out[0])
    k = np.array([r.strike for r in usable])[:, None]
    iv = np.array([r.iv for r in usable])[:, None]
    oi = np.array([r.open_interest for r in usable])[:, None]
    sign = np.array([1.0 if r.option_type == "C" else -1.0 for r in usable])[:, None]
    t = max(t_years, MIN_T_YEARS)
    sq = iv * math.sqrt(t)
    d1 = (np.log(s[None, :] / k) + 0.5 * iv * iv * t) / sq
    gamma = norm.pdf(d1) / (s[None, :] * sq)
    total = (sign * gamma * oi).sum(axis=0) * s * CONTRACT_MULTIPLIER
    return total if np.ndim(spot) else float(total[0])


def find_zero_gamma(rows: Sequence[OptionRow], spot: float, t_years: float,
                    sweep_pct: float = 0.04, step: float = 0.25) -> float | None:
    """Spot level where total GEX crosses zero, nearest to the current spot."""
    levels = np.arange(spot * (1 - sweep_pct), spot * (1 + sweep_pct) + step, step)
    values = total_gex_at(rows, levels, t_years)
    if not np.any(values):
        return None
    crossings: list[float] = []
    for i in range(1, len(levels)):
        a, b = float(values[i - 1]), float(values[i])
        if a == 0:
            crossings.append(float(levels[i - 1]))
        elif (a < 0) != (b < 0):
            # Linear interpolation between the two bracketing spot levels.
            frac = abs(a) / (abs(a) + abs(b))
            crossings.append(float(levels[i - 1] + frac * (levels[i] - levels[i - 1])))
    if not crossings:
        return None
    return min(crossings, key=lambda x: abs(x - spot))


def cumulative_flip(net: dict[float, float], spot: float) -> float | None:
    """Fallback: strike where the cumulative (low-to-high) net GEX changes sign."""
    strikes = sorted(net)
    running = 0.0
    prev_sign = None
    flips: list[float] = []
    for k in strikes:
        running += net[k]
        sign = running >= 0
        if prev_sign is not None and sign != prev_sign:
            flips.append(k)
        prev_sign = sign
    if not flips:
        return None
    return min(flips, key=lambda x: abs(x - spot))


def atm_iv(rows: Sequence[OptionRow], spot: float) -> float | None:
    """Average IV of the call and put nearest to spot."""
    with_iv = [r for r in rows if r.iv and r.iv > 0]
    if not with_iv:
        return None
    nearest = min(abs(r.strike - spot) for r in with_iv)
    ivs = [r.iv for r in with_iv if abs(r.strike - spot) == nearest]
    return float(sum(ivs) / len(ivs))


def compute_gex(rows: Sequence[OptionRow], spot: float, expiration: datetime,
                now: datetime) -> GexReport:
    t = years_until(expiration, now)
    net = net_gex_by_strike(rows, spot)
    total = float(sum(net.values()))

    call_wall = max(net, key=net.get) if net and max(net.values()) > 0 else None
    put_wall = min(net, key=net.get) if net and min(net.values()) < 0 else None

    zero = find_zero_gamma(rows, spot, t)
    if zero is None:
        zero = cumulative_flip(net, spot)

    iv = atm_iv(rows, spot)
    sd = spot * iv * math.sqrt(max(t, MIN_T_YEARS)) if iv else None

    return GexReport(
        timestamp=now,
        expiration=expiration.date().isoformat(),
        spot=spot,
        total_gex=total,
        regime="POSITIVE" if total >= 0 else "NEGATIVE",
        zero_gamma=zero,
        call_wall=call_wall,
        put_wall=put_wall,
        net_by_strike=net,
        atm_iv=iv,
        one_sd_move=sd,
    )


def round_to_strike(level: float, strikes: Sequence[float]) -> float:
    return min(strikes, key=lambda k: abs(k - level))
