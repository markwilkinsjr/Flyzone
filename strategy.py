"""
Entry decision logic. Pure functions: no network, no clock, fully testable.

Decision sequence (agreed spec):

1. VIX gate (8:30 AM)        -> regime, structure, wing width, debit cap.
2. Directional gate (9:00)   -> single fly only: which wall to target.
3. Distance gate             -> PoT of nearest long strike >= 67 %,
                                spot must not already be inside the tent.
4. Price gate                -> strike may shift away from the wall to fit
                                the debit cap. Single fly: the 67 % PoT gate
                                bounds how far it can shift. Batman: the
                                centre must stay within 1 SD of spot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from config import RegimeParams
from gex_engine import GexReport, OptionRow, probability_of_touch, round_to_strike
from indicators.supertrend import SuperTrendResult
from models import Fly, FlyLeg

# --------------------------------------------------------------------------- #
# Step 2: directional gate
# --------------------------------------------------------------------------- #
@dataclass
class DirectionDecision:
    condition: str              # "A", "B", "C", "D" or "STANDBY"
    option_type: str | None     # "C" / "P" / None
    target_level: float | None
    trend: str                  # "BULLISH" / "BEARISH"
    gate_active: bool
    reason: str


def directional_gate(report: GexReport, trend: SuperTrendResult,
                     require_momentum_gate: bool = True) -> DirectionDecision:
    bullish = trend.is_bullish
    gate_ok = trend.momentum_gate_active
    trend_name = "BULLISH" if bullish else "BEARISH"

    def standby(reason: str, cond: str = "STANDBY") -> DirectionDecision:
        return DirectionDecision(cond, None, None, trend_name, gate_ok, reason)

    if report.zero_gamma is None:
        return standby("Zero-gamma line could not be located; no structural anchor.")
    if require_momentum_gate and not gate_ok:
        return standby(
            f"SuperTrend {trend_name} but momentum gate inactive "
            f"(norm mom {trend.last_mom_norm:+.2f})."
        )

    above = report.spot > report.zero_gamma
    spot = report.spot

    if above and bullish:
        if report.call_wall is None or report.call_wall <= spot:
            return standby("Condition A but no call wall above spot.")
        return DirectionDecision("A", "C", report.call_wall, trend_name, gate_ok,
                                 "Positive-gamma grind: call fly at the Call Wall.")
    if above and not bullish:
        # Mean-reversion: target whichever of zero-gamma / put wall is the
        # nearest level below spot.
        candidates = [x for x in (report.zero_gamma, report.put_wall)
                      if x is not None and x < spot]
        if not candidates:
            return standby("Condition B but no zero-gamma / put wall below spot.")
        target = max(candidates)
        return DirectionDecision("B", "P", target, trend_name, gate_ok,
                                 "Positive-gamma rejection: put fly at Zero Gamma / Put Wall.")
    if not above and not bullish:
        if report.put_wall is None or report.put_wall >= spot:
            return standby("Condition C but no put wall below spot.")
        return DirectionDecision("C", "P", report.put_wall, trend_name, gate_ok,
                                 "Negative-gamma flush: put fly at the Put Wall.")
    return standby("Divergence trap: below zero gamma with a bullish trend.", "D")


# --------------------------------------------------------------------------- #
# Fly construction & pricing
# --------------------------------------------------------------------------- #
@dataclass
class ChainView:
    """Option rows for one expiration, indexed for quick fly construction."""
    rows: Sequence[OptionRow]
    expiration: str
    _idx: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        for r in self.rows:
            self._idx[(r.strike, r.option_type)] = r

    def get(self, strike: float, option_type: str) -> OptionRow | None:
        return self._idx.get((strike, option_type))

    def strikes(self, option_type: str) -> list[float]:
        return sorted({k for (k, t) in self._idx if t == option_type})


def build_fly(chain: ChainView, option_type: str, center: float,
              width: float) -> Fly | None:
    lower, upper = center - width, center + width
    rows = [chain.get(k, option_type) for k in (lower, center, upper)]
    if any(r is None for r in rows):
        return None
    lo, mid, hi = rows
    return Fly(
        option_type=option_type, lower=lower, center=center, upper=upper,
        expiration=chain.expiration,
        legs=[
            FlyLeg(lo.symbol, lo.streamer_symbol, lower, option_type, +1),
            FlyLeg(mid.symbol, mid.streamer_symbol, center, option_type, -2),
            FlyLeg(hi.symbol, hi.streamer_symbol, upper, option_type, +1),
        ],
    )


def fly_mid(chain: ChainView, fly: Fly) -> float | None:
    """Mid debit of the fly from the chain snapshot (points per spread)."""
    total = 0.0
    for leg in fly.legs:
        row = chain.get(leg.strike, leg.option_type)
        if row is None or row.mid is None:
            return None
        total += leg.ratio * row.mid
    return round(total, 4)


def nearest_long_strike(fly: Fly) -> float:
    return fly.lower if fly.option_type == "C" else fly.upper


# --------------------------------------------------------------------------- #
# Steps 3 & 4 for the single directional fly
# --------------------------------------------------------------------------- #
@dataclass
class FlyPick:
    fly: Fly | None
    mid: float | None
    pot: float | None
    shifted_by: float
    reason: str


def select_single_fly(chain: ChainView, option_type: str, target_level: float,
                      spot: float, params: RegimeParams,
                      t_years: float, pot_min: float) -> FlyPick:
    width = float(params.wing_width)
    cap = float(params.debit_cap)
    strikes = chain.strikes(option_type)
    if not strikes:
        return FlyPick(None, None, None, 0, "No strikes in chain.")
    wall = round_to_strike(target_level, strikes)

    # Distance gate on the wall-centred fly first.
    base = build_fly(chain, option_type, wall, width)
    if base is None:
        return FlyPick(None, None, None, 0, f"Strikes for a fly at {wall:g} not listed.")
    near = nearest_long_strike(base)
    if (option_type == "C" and spot >= near) or (option_type == "P" and spot <= near):
        return FlyPick(None, None, None, 0,
                       f"Spot {spot:.2f} already at/inside the tent (long {near:g}).")

    # Walk away from spot one strike at a time until the debit fits the cap.
    # Each step lowers the probability of touch, so the PoT gate is what
    # stops the walk.
    direction = 1 if option_type == "C" else -1
    ordered = [k for k in strikes if (k - wall) * direction >= 0]
    ordered.sort(key=lambda k: abs(k - wall))
    last_reason = ""
    for center in ordered:
        fly = build_fly(chain, option_type, center, width)
        if fly is None:
            continue
        near_row = chain.get(nearest_long_strike(fly), option_type)
        pot = probability_of_touch(
            spot, nearest_long_strike(fly), t_years,
            iv=near_row.iv if near_row else None,
            delta=near_row.delta if near_row else None,
            option_type=option_type,
        )
        if pot < pot_min:
            last_reason = (f"PoT of long {nearest_long_strike(fly):g} is "
                           f"{pot:.0%} (< {pot_min:.0%}); target too far.")
            break
        mid = fly_mid(chain, fly)
        if mid is None or mid <= 0:
            last_reason = f"No usable quote for fly at {center:g}."
            continue
        if mid <= cap:
            shift = abs(center - wall)
            note = "OK" if shift == 0 else f"Shifted {shift:g} pts to fit cap."
            return FlyPick(fly, mid, pot, shift, note)
        last_reason = f"Fly at {center:g} mid {mid:.2f} > cap {cap:.2f}."
    return FlyPick(None, None, None, 0, last_reason or "No candidate passed.")


# --------------------------------------------------------------------------- #
# Batman wing selection (price rules only)
# --------------------------------------------------------------------------- #
def select_batman_wing(chain: ChainView, option_type: str, wall: float | None,
                       spot: float, one_sd: float, params: RegimeParams) -> FlyPick:
    """
    Pick the OTM fly whose mid prices inside [debit_floor, debit_cap] and
    whose centre is within 1 SD of spot, preferring the one closest to the wall.
    """
    width = float(params.wing_width)
    lo_px, hi_px = float(params.debit_floor), float(params.debit_cap)
    anchor = wall if wall is not None else spot
    best: FlyPick | None = None
    for center in chain.strikes(option_type):
        otm = center > spot if option_type == "C" else center < spot
        if not otm or abs(center - spot) > one_sd:
            continue
        fly = build_fly(chain, option_type, center, width)
        if fly is None:
            continue
        mid = fly_mid(chain, fly)
        if mid is None or not (lo_px <= mid <= hi_px):
            continue
        pick = FlyPick(fly, mid, None, abs(center - anchor),
                       "OK" if wall is not None and center == wall
                       else f"{abs(center - anchor):g} pts from wall to fit window.")
        if best is None or pick.shifted_by < best.shifted_by:
            best = pick
    if best is None:
        return FlyPick(None, None, None, 0,
                       f"No {option_type} fly within 1 SD prices in "
                       f"${lo_px:.2f}-${hi_px:.2f}.")
    return best


def debit_to_str(x: Decimal | float) -> str:
    return f"{Decimal(str(x)).quantize(Decimal('0.01'))}"

