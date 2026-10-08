"""
Configuration for the Flyzone XSP butterfly bot.

Two kinds of settings live here:

* ``Settings``  - secrets and switches loaded from the ``.env`` file
                  (validated on startup so a typo fails fast, not at 9:00 AM).
* ``Strategy``  - the trading rules agreed with the user, kept as plain
                  constants so they are easy to read and tweak in one place.

Everything time-related is expressed in US Central time (``America/Chicago``),
which follows daylight-saving automatically, so "9:00 AM CDT" in summer and
"9:00 AM CST" in winter are both simply ``time(9, 0)`` here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("America/Chicago")


# --------------------------------------------------------------------------- #
# Secrets / runtime switches (.env)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    sandbox_client_secret: str
    sandbox_refresh_token: str
    sandbox_account_number: str
    is_sandbox: bool
    prod_client_secret: str
    prod_refresh_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    dry_run: bool
    log_level: str
    state_file: Path
    live_account_number: str = ""

    @property
    def order_account_number(self) -> str:
        return self.sandbox_account_number if self.is_sandbox else self.live_account_number

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Settings":
        """Read ``.env`` (if present) and the process environment, then validate."""
        load_dotenv(env_path or PROJECT_ROOT / ".env")

        def req(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RuntimeError(f"Missing required environment variable: {name}")
            return value

        def flag(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None or raw.strip() == "":
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        settings = cls(
            sandbox_client_secret=req("TASTYTRADE_SANDBOX_CLIENT_SECRET"),
            sandbox_refresh_token=req("TASTYTRADE_SANDBOX_REFRESH_TOKEN"),
            sandbox_account_number=req("TASTYTRADE_SANDBOX_ACCOUNT_NUMBER"),
            is_sandbox=flag("TASTYTRADE_IS_SANDBOX", True),
            prod_client_secret=req("TASTYTRADE_PROD_CLIENT_SECRET"),
            prod_refresh_token=req("TASTYTRADE_PROD_REFRESH_TOKEN"),
            telegram_bot_token=req("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=req("TELEGRAM_CHAT_ID"),
            dry_run=flag("DRY_RUN", True),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            state_file=Path(os.getenv("STATE_FILE", "state.json")),
            live_account_number=os.getenv("TASTYTRADE_LIVE_ACCOUNT_NUMBER", "").strip(),
        )
        if not settings.is_sandbox and not settings.live_account_number:
            raise RuntimeError(
                "TASTYTRADE_IS_SANDBOX=false requires TASTYTRADE_LIVE_ACCOUNT_NUMBER."
            )
        if not settings.is_sandbox and settings.dry_run is False:
            # A deliberate speed bump: live trading requires both flags flipped
            # AND an explicit acknowledgement variable.
            if os.getenv("I_UNDERSTAND_THIS_IS_LIVE_TRADING", "").lower() != "yes":
                raise RuntimeError(
                    "TASTYTRADE_IS_SANDBOX=false with DRY_RUN=false would route REAL "
                    "orders. Set I_UNDERSTAND_THIS_IS_LIVE_TRADING=yes to confirm."
                )
        return settings


# --------------------------------------------------------------------------- #
# Strategy rules (agreed spec)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegimeParams:
    """Structure parameters for one VIX regime."""

    name: str
    structure: str            # "BATMAN" (dual wing, 1DTE) or "SINGLE_FLY" (0DTE)
    dte: int                  # days to expiration of the traded contract
    wing_width: Decimal       # XSP points between long and short strikes
    debit_floor: Decimal      # lowest limit we start at (Batman only)
    debit_cap: Decimal        # 10% of width - never bid above this
    entry_time: time          # when the entry job fires (Central)
    cancel_time: time         # unfilled orders are cancelled here (Central)


@dataclass(frozen=True)
class Strategy:
    underlying: str = "XSP"
    vix_symbol: str = "VIX"
    contract_multiplier: int = 100
    quantity: int = 1

    # VIX regime boundaries (inclusive lower bound)
    vix_normal: Decimal = Decimal("17")
    vix_chaos: Decimal = Decimal("28")

    zombieland: RegimeParams = RegimeParams(
        name="ZOMBIELAND", structure="BATMAN", dte=1,
        wing_width=Decimal("2"), debit_floor=Decimal("0.15"),
        debit_cap=Decimal("0.20"), entry_time=time(14, 0), cancel_time=time(14, 45),
    )
    normal: RegimeParams = RegimeParams(
        name="NORMAL", structure="SINGLE_FLY", dte=0,
        wing_width=Decimal("3"), debit_floor=Decimal("0.30"),
        debit_cap=Decimal("0.30"), entry_time=time(9, 0), cancel_time=time(10, 0),
    )
    chaos: RegimeParams = RegimeParams(
        name="CHAOS", structure="SINGLE_FLY", dte=0,
        wing_width=Decimal("4"), debit_floor=Decimal("0.40"),
        debit_cap=Decimal("0.40"), entry_time=time(9, 0), cancel_time=time(10, 0),
    )

    # Market session (Central)
    market_open: time = time(8, 30)
    market_close: time = time(15, 0)
    vix_gate_time: time = time(8, 31)      # one minute after the open for a fresh print
    gex_hours: tuple[int, ...] = (9, 10, 11, 12, 13, 14, 15)   # hourly at :00
    time_stop_recommendation: time = time(14, 15)

    # Directional gate (single fly)
    require_momentum_gate: bool = True   # trend must also clear the momentum gate
    pot_min: float = 0.67                # probability-of-touch of nearest long strike

    # Exit rules (winners only)
    profit_lock_multiple: Decimal = Decimal("2.0")   # arm floor at +100 % ROI
    trail_pct_after_time_stop: Decimal = Decimal("0.20")  # 20 % below high-water mark
    exit_limit_buffer: Decimal = Decimal("0.05")     # fallback if market order rejected
    exit_limit_step_seconds: int = 15

    # Monitoring cadence
    monitor_interval_seconds: int = 30
    order_poll_seconds: int = 15
    chase_step: Decimal = Decimal("0.01")
    chase_interval_seconds: int = 60

    # Indicator (SWMA Momentum SuperTrend, "Intraday" preset)
    candle_interval: str = "5m"
    candle_lookback_days: int = 4
    supertrend: dict = field(default_factory=lambda: {
        "swma_smooth": 8, "atr_len": 10, "factor": 2.2, "mom_len": 7,
        "use_smooth": True, "smooth_len": 3, "mom_gate": 0.5,
    })

    # Calendar
    skip_early_close_days: bool = True

    def regime_for_vix(self, vix: Decimal | float) -> RegimeParams:
        """Map a VIX reading to the regime that governs today's structure."""
        v = Decimal(str(vix))
        if v < self.vix_normal:
            return self.zombieland
        if v < self.vix_chaos:
            return self.normal
        return self.chaos


STRATEGY = Strategy()
