"""
Position valuation and exit rules. Winners only: a losing trade is never
touched and simply expires.

Rules, evaluated every monitoring tick for each filled wing:

1. +100 % profit floor
   When the wing's value first reaches 2.0 x entry debit, the floor is
   "armed" at exactly 2.0 x entry. It never moves down.

2. 20 % trail after 2:15 PM CT
   From 2:15 PM on expiration day, the floor ratchets up to 80 % of the high-water mark,
   but only once that level is itself above the entry debit (a trail that
   would lock in a loss is pointless under the "winners only" rule).
   Example: entry 0.30, peak 0.90 -> floor 0.72; peak 1.10 -> floor 0.88.

3. Hold zone
   While spot is inside the hold zone the bot never closes - a pin at the
   centre strike is the best outcome. The hold zone is configurable:
     "tent" (default) - anywhere between the two long strikes
     "core"           - within half a wing width of the centre strike
   With "tent", the floor/trail can only fire after spot leaves the tent.

4. Exit triggers (spot outside the hold zone)
   a. value <= active floor           -> PROFIT_FLOOR / TRAILING_LOCK
   b. spot outside the tent AND the
      trade is a winner (value > entry
      or the floor was ever armed)    -> LEFT_TENT
   Anything else holds to expiration.

All exits are sent as market orders (see tasty_client.OrderRouter).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from config import STRATEGY, Strategy
from models import Fly, FlyQuote, Wing, d

HOLD_ZONE = "tent"          # "tent" or "core" - see module docstring
VALUATION_BASIS = "mid"     # "mid" (matches the Tastytrade P&L display) or "natural"


@dataclass
class ExitDecision:
    exit: bool
    reason: str | None = None
    events: list[str] = field(default_factory=list)   # FLOOR_ARMED, TRAIL_ON, TRAIL_RAISED
    value: Decimal | None = None


def value_fly(fly: Fly, quotes: dict[str, tuple[float | None, float | None]]) -> FlyQuote:
    """
    Value a long butterfly from leg bid/ask quotes.

    mid          = sum(ratio * leg_mid)
    natural_sell = long legs at bid  - short legs at ask   (closing at market)
    natural_buy  = long legs at ask  - short legs at bid   (opening at market)
    """
    mid = nat_sell = nat_buy = 0.0
    for leg in fly.legs:
        bid, ask = quotes.get(leg.streamer_symbol, (None, None))
        if bid is None or ask is None:
            return FlyQuote(None, None, None)
        leg_mid = (bid + ask) / 2
        mid += leg.ratio * leg_mid
        if leg.ratio > 0:
            nat_sell += leg.ratio * bid
            nat_buy += leg.ratio * ask
        else:
            nat_sell += leg.ratio * ask
            nat_buy += leg.ratio * bid
    return FlyQuote(round(max(mid, 0.0), 4), round(max(nat_sell, 0.0), 4),
                    round(max(nat_buy, 0.0), 4))


def evaluate_wing(wing: Wing, spot: float, quote: FlyQuote, now: datetime,
                  strategy: Strategy = STRATEGY, hold_zone: str = HOLD_ZONE,
                  basis: str = VALUATION_BASIS) -> ExitDecision:
    """
    Update the wing's high-water mark / floor in place and decide whether to
    exit. Mutates ``wing`` so the caller can persist the new state.
    """
    if wing.status != "FILLED" or wing.entry_debit is None:
        return ExitDecision(False)

    raw = quote.mid if basis == "mid" else quote.natural_sell
    if raw is None:
        return ExitDecision(False, events=["NO_QUOTE"])
    value = d(raw).quantize(Decimal("0.0001"))
    entry = d(wing.entry_debit)
    events: list[str] = []

    # ---- High-water mark ------------------------------------------------- #
    hwm = max(d(wing.high_water or "0"), value)
    wing.high_water = str(hwm)

    # ---- Rule 1: arm the +100 % floor (one-way latch) -------------------- #
    lock_level = entry * strategy.profit_lock_multiple
    if not wing.floor_armed and value >= lock_level:
        wing.floor_armed = True
        wing.floor = str(max(d(wing.floor or "0"), lock_level))
        events.append("FLOOR_ARMED")

    # ---- Rule 2: 20 % trail after 2:15 PM on expiration day --------------- #
    # (A 1-DTE Batman opened at 2:00 PM must not start trailing on day one.)
    expiration_day = now.date().isoformat() == wing.fly.expiration
    if expiration_day and now.time() >= strategy.time_stop_recommendation:
        trail_level = (hwm * (Decimal("1") - strategy.trail_pct_after_time_stop)
                       ).quantize(Decimal("0.01"))
        if trail_level > entry:
            current = d(wing.floor or "0")
            if trail_level > current:
                wing.floor = str(trail_level)
                events.append("TRAIL_RAISED" if wing.trail_active else "TRAIL_ON")
            wing.trail_active = True

    # ---- Rule 3: hold zone ----------------------------------------------- #
    fly = wing.fly
    in_zone = fly.in_tent(spot) if hold_zone == "tent" else fly.in_core(spot)
    if in_zone:
        return ExitDecision(False, events=events, value=value)

    # ---- Rule 4: exit triggers ------------------------------------------- #
    floor = d(wing.floor) if wing.floor else None
    if floor is not None and value <= floor:
        reason = "TRAILING_LOCK" if wing.trail_active else "PROFIT_FLOOR"
        return ExitDecision(True, reason, events, value)

    is_winner = value > entry or wing.floor_armed
    if not fly.in_tent(spot) and is_winner:
        return ExitDecision(True, "LEFT_TENT", events, value)

    return ExitDecision(False, events=events, value=value)


def realized_pnl(entry: Decimal, exit_px: Decimal, qty: int,
                 multiplier: int = 100) -> tuple[Decimal, Decimal]:
    """Dollar P&L and ROI fraction for a long debit spread."""
    pnl = (exit_px - entry) * multiplier * qty
    roi = (exit_px - entry) / entry if entry else Decimal("0")
    return pnl.quantize(Decimal("0.01")), roi


def time_stop_recommendation(wing: Wing, spot: float, value: Decimal | None) -> str:
    """Plain-language 2:15 PM recommendation for one wing."""
    fly = wing.fly
    entry = d(wing.entry_debit or "0")
    if value is None:
        return "No quote available; holding."
    if fly.in_tent(spot):
        dist = abs(spot - fly.center)
        return (f"Spot {spot:.2f} is inside the tent ({dist:.2f} pts from the "
                f"{fly.center:g} pin). Recommendation: HOLD for the pin.")
    if value > entry:
        return ("Outside the tent and profitable. Recommendation: take profits "
                "(the bot will close it on the next tick).")
    return "Outside the tent and below entry. Losers are not managed; letting it expire."
