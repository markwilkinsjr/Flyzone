"""
Crash-safe persistence in ``state.json``.

Everything needed to resume after a restart lives here: today's VIX regime,
which scheduled jobs already ran, the active trade set (orders, entry debit,
high-water mark, armed floor, trail), blocked dates, and the paused flag.
Writes are atomic (temp file + rename) so a crash mid-write cannot corrupt it.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from models import TradeSet, tradeset_from_dict, tradeset_to_dict


@dataclass
class BotState:
    day: str = ""                          # ISO date the daily fields refer to
    vix: float | None = None
    regime: str | None = None
    plan: str = "Waiting for the 8:30 AM VIX gate."
    jobs_done: list[str] = field(default_factory=list)
    paused: bool = False
    tradeset: TradeSet | None = None
    blocked_dates: list[str] = field(default_factory=list)   # no new entries these days
    history: list[dict] = field(default_factory=list)        # closed trade sets


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        raw = json.loads(self.path.read_text())
        ts = raw.pop("tradeset", None)
        state = BotState(**raw)
        state.tradeset = tradeset_from_dict(ts) if ts else None
        return state

    def save(self, state: BotState) -> None:
        payload = dict(state.__dict__)
        payload["tradeset"] = tradeset_to_dict(state.tradeset) if state.tradeset else None
        payload["history"] = state.history[-200:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
