"""
Plain data models shared across modules, all JSON-serialisable so they can be
written to ``state.json`` and survive a restart.

Butterfly construction (long fly, 1 contract):

    Call fly centred at K, width W:   +1 (K-W) C   -2 K C   +1 (K+W) C
    Put  fly centred at K, width W:   +1 (K+W) P   -2 K P   +1 (K-W) P

Both legs at K-W and K+W are bought (the "long wings"), two contracts at K
are sold (the "body"). Max value is W at expiration with the index pinned at
K; the position is worthless at or beyond either long strike. The region
between the long strikes is the "tent".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any


def d(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


@dataclass
class FlyLeg:
    symbol: str                # OCC symbol for orders
    streamer_symbol: str       # dxfeed symbol for quotes
    strike: float
    option_type: str           # "C" / "P"
    ratio: int                 # +1 long wing, -2 short body


@dataclass
class Fly:
    option_type: str           # "C" call fly, "P" put fly
    lower: float
    center: float
    upper: float
    expiration: str            # ISO date
    legs: list[FlyLeg]

    @property
    def width(self) -> float:
        return self.center - self.lower

    def in_tent(self, spot: float) -> bool:
        return self.lower < spot < self.upper

    def in_core(self, spot: float) -> bool:
        """Middle half of the tent: within width/2 of the centre strike."""
        return abs(spot - self.center) <= self.width / 2

    def describe(self) -> str:
        t = self.option_type
        return (f"+1 {self.lower:g}{t} / -2 {self.center:g}{t} / "
                f"+1 {self.upper:g}{t}  exp {self.expiration}")

    def value_at_expiry(self, spot: float) -> float:
        """Intrinsic value per spread (points) if settled at ``spot``."""
        def intrinsic(k: float) -> float:
            if self.option_type == "C":
                return max(spot - k, 0.0)
            return max(k - spot, 0.0)
        return max(intrinsic(self.lower) - 2 * intrinsic(self.center)
                   + intrinsic(self.upper), 0.0)


@dataclass
class FlyQuote:
    """Live valuation of one fly from leg quotes (per spread, in points)."""
    mid: float | None
    natural_sell: float | None   # what a market sell would roughly receive
    natural_buy: float | None    # what a market buy would roughly pay


@dataclass
class Wing:
    """One butterfly order/position and its full lifecycle."""
    fly: Fly
    status: str = "PENDING"      # PENDING, WORKING, FILLED, CANCELLED, CLOSING, CLOSED, EXPIRED
    order_id: int | None = None
    limit: str | None = None     # current limit debit (Decimal as str)
    entry_debit: str | None = None
    filled_at: str | None = None
    high_water: str | None = None
    floor_armed: bool = False    # +100 % profit floor unlocked
    floor: str | None = None     # active exit floor (Decimal as str)
    trail_active: bool = False   # 20 % trail after 2:15 PM
    exit_order_id: int | None = None
    exit_price: str | None = None
    exit_reason: str | None = None
    closed_at: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in {"FILLED", "CLOSING"}

    @property
    def is_working(self) -> bool:
        return self.status in {"PENDING", "WORKING"}


@dataclass
class TradeSet:
    """A Batman (two wings) or a single fly (one wing)."""
    structure: str               # "BATMAN" or "SINGLE_FLY"
    regime: str
    opened_on: str               # ISO date the entry job ran
    expiration: str
    wings: list[Wing]
    cancel_at: str               # ISO datetime: unfilled orders cancelled here
    condition: str | None = None  # directional gate condition A/B/C for single fly
    structural_alert_sent: bool = False
    time_stop_sent: bool = False

    @property
    def has_open(self) -> bool:
        return any(w.is_open for w in self.wings)

    @property
    def has_working(self) -> bool:
        return any(w.is_working for w in self.wings)

    @property
    def is_active(self) -> bool:
        return self.has_open or self.has_working


# --------------------------------------------------------------------------- #
# (De)serialisation helpers for state.json
# --------------------------------------------------------------------------- #
def tradeset_to_dict(ts: TradeSet) -> dict:
    return asdict(ts)


def tradeset_from_dict(raw: dict) -> TradeSet:
    wings = []
    for w in raw["wings"]:
        fly_raw = dict(w["fly"])
        fly_raw["legs"] = [FlyLeg(**leg) for leg in fly_raw["legs"]]
        wing_raw = dict(w)
        wing_raw["fly"] = Fly(**fly_raw)
        wings.append(Wing(**wing_raw))
    raw = dict(raw)
    raw["wings"] = wings
    return TradeSet(**raw)
