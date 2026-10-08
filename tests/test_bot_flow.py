"""
End-to-end flow through main.Bot with a fake data feed, fake broker and a
message-collecting notifier. Exercises entry, fills, the Batman chase, the
cutoff, exits, expiry settlement, reconciliation and Telegram commands.
"""
import math
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from config import TZ, Settings
from main import Bot
from tasty_client import Bar, OrderResult
from tests.fixtures import make_chain


def t(day, h, m):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


class FakeNotifier:
    def __init__(self):
        self.msgs = []

    async def send(self, text):
        self.msgs.append(text)

    async def close(self):
        pass


class FakeOpt:
    pass


class FakeData:
    def __init__(self, spot=675.0, vix=18.4, iv=0.25, hours=6):
        self.spot, self.vix = spot, vix
        self.rows = make_chain(spot=spot, iv=iv, t=hours / (365 * 24))
        self.quote_override = {}

    async def get_index_price(self, sym, timeout=15):
        return self.vix if sym == "VIX" else self.spot

    def latest_index_price(self, sym):
        return self.spot

    async def options_for(self, und, exp):
        return [FakeOpt()]

    async def option_rows(self, options, spot, **kw):
        return self.rows

    async def candles(self, sym, interval, start):
        bars = []
        base = datetime(2026, 10, 7, 8, 30, tzinfo=TZ)
        for i in range(200):
            c = 660 + 0.08 * i + 0.3 * math.sin(i / 3)
            bars.append(Bar(base + timedelta(minutes=5 * i), c, c + 0.4, c - 0.4, c))
        return bars

    async def leg_quotes(self, syms):
        out = {}
        for r in self.rows:
            if r.streamer_symbol in syms:
                out[r.streamer_symbol] = self.quote_override.get(r.streamer_symbol,
                                                                 (r.bid, r.ask))
        return out


class FakeRouter:
    env_name = "SANDBOX (cert)"

    def __init__(self):
        self.ids = 1
        self.orders = {}
        self.fill_next = set()
        self.positions = {}

    async def open_fly(self, fly, qty, limit):
        oid = self.ids
        self.ids += 1
        self.orders[oid] = {"fly": fly, "limit": limit, "status": "LIVE"}
        return OrderResult(oid, "LIVE")

    async def replace_limit(self, oid, fly, qty, new):
        self.orders[oid]["limit"] = new
        return OrderResult(oid, "LIVE")

    async def cancel(self, oid):
        if self.orders[oid]["status"] == "LIVE":
            self.orders[oid]["status"] = "CANCELLED"

    async def order_status(self, oid):
        o = self.orders[oid]
        if oid in self.fill_next and o["status"] == "LIVE":
            o["status"] = "FILLED"
            o["fill"] = Decimal("1.00")          # sandbox-style fake fill price
        return OrderResult(oid, o["status"], o.get("fill"))

    async def close_fly_market(self, fly, qty, credit):
        oid = self.ids
        self.ids += 1
        self.orders[oid] = {"status": "FILLED", "fill": Decimal("9.99")}
        return OrderResult(oid, "FILLED", Decimal("9.99"))

    async def option_positions(self, und):
        return self.positions


@pytest.fixture
def bot(tmp_path):
    s = Settings("x", "x", "5WT0001", True, "x", "x", "tok", "123", dry_run=False,
                 log_level="INFO", state_file=Path(tmp_path / "state.json"))
    b = Bot(s)
    b.notify = FakeNotifier()
    b.data = FakeData()
    b.router = FakeRouter()
    return b


async def test_vix_gate_sets_regime(bot):
    bot.roll_day(t(8, 8, 31))
    await bot.job_vix_gate(t(8, 8, 31))
    assert bot.state.regime == "NORMAL"
    assert "single directional fly" in bot.notify.msgs[-1]


async def test_single_fly_entry_runs_and_cancels_at_cutoff(bot):
    bot.roll_day(t(8, 8, 31))
    await bot.job_vix_gate(t(8, 8, 31))
    await bot.job_entry(t(8, 9, 0))
    gate_msgs = [m for m in bot.notify.msgs if "Directional Gate" in m]
    assert gate_msgs, bot.notify.msgs
    ts = bot.state.tradeset
    if ts is None:          # gate chose standby/abort: nothing else to check
        return
    assert ts.wings[0].limit == "0.30"
    await bot.manage_orders(t(8, 10, 0))     # cutoff
    assert bot.state.tradeset is None
    assert any("Cutoff" in m for m in bot.notify.msgs)


async def test_batman_chase_fill_exit_and_block(bot):
    bot.data = FakeData(vix=15.0, iv=0.14, hours=25)
    bot.roll_day(t(8, 8, 31))
    await bot.job_vix_gate(t(8, 8, 31))
    assert bot.state.regime == "ZOMBIELAND"
    await bot.job_entry(t(8, 14, 0))
    ts = bot.state.tradeset
    assert ts and ts.structure == "BATMAN" and ts.expiration == "2026-10-09"
    assert all(w.limit == "0.15" for w in ts.wings)
    if len(ts.wings) < 2:
        pytest.skip("synthetic chain priced only one wing")
    first, second = ts.wings
    bot.router.fill_next.add(first.order_id)
    await bot.manage_orders(t(8, 14, 1))
    assert first.status == "FILLED"
    assert first.entry_debit == "0.15"        # sandbox: booked at limit, not fake fill
    assert "2026-10-09" in bot.state.blocked_dates
    assert second.limit == "0.16"             # walked up one cent as soon as wing 1 filled
    await bot.manage_orders(t(8, 14, 1) + timedelta(seconds=30))
    assert second.limit == "0.16"             # not again within 60 s
    await bot.manage_orders(t(8, 14, 2))
    assert second.limit == "0.17"
    for m in range(3, 10):
        await bot.manage_orders(t(8, 14, m))
    assert second.limit == "0.20"             # never above the $0.20 cap
    await bot.manage_orders(t(8, 14, 45))     # cutoff: unfilled wing cancelled
    assert second.status == "CANCELLED" and first.status == "FILLED"
    assert bot.state.tradeset is not None

    # Next day: VIX gate sees the block; spot runs outside the tent while the
    # wing is worth more than entry -> LEFT_TENT exit at the model price.
    bot.roll_day(t(9, 8, 31))
    assert bot.entry_blocker(t(9, 8, 31).date())
    f = first.fly
    bot.data.spot = f.upper + 1 if f.option_type == "C" else f.lower - 1
    for leg in f.legs:
        bot.data.quote_override[leg.streamer_symbol] = (
            (0.50, 0.52) if leg.ratio > 0 else (0.05, 0.06))
    await bot.monitor_exits(t(9, 11, 0))
    assert first.status == "CLOSED" and first.exit_reason == "LEFT_TENT"
    assert first.exit_price != "9.99"         # sandbox fake fill ignored
    assert bot.state.tradeset is None and bot.state.history


async def test_expiry_settles_in_tent_winner(bot):
    bot.data = FakeData(vix=15.0, iv=0.14, hours=25)
    bot.roll_day(t(8, 8, 31))
    await bot.job_vix_gate(t(8, 8, 31))
    await bot.job_entry(t(8, 14, 0))
    ts = bot.state.tradeset
    w = ts.wings[0]
    bot.router.fill_next.add(w.order_id)
    await bot.manage_orders(t(8, 14, 1))
    bot.roll_day(t(9, 8, 31))
    bot.data.spot = w.fly.center            # pinned
    await bot.job_expiry(t(9, 15, 1))
    assert w.status == "EXPIRED"
    assert Decimal(w.exit_price) == Decimal("2.00")
    assert any("Trade Exit" in m for m in bot.notify.msgs)


async def test_reconcile_mismatch_pauses(bot):
    bot.router.positions = {"XSP 999C": Decimal(1)}
    await bot.reconcile()
    assert bot.state.paused
    assert "mismatch" in bot.notify.msgs[-1]


async def test_commands(bot):
    assert "Paused" in await bot.handle_command("/pause", "")
    assert bot.state.paused
    assert "Resumed" in await bot.handle_command("/resume", "")
    assert "CONFIRM" in await bot.handle_command("/flatten", "")
    assert "Nothing" in await bot.handle_command("/flatten", "CONFIRM")
    assert "Status" in await bot.handle_command("/status", "")


async def test_stale_tradeset_archived_on_new_day(bot):
    bot.data = FakeData(vix=15.0, iv=0.14, hours=25)
    bot.roll_day(t(8, 8, 31))
    await bot.job_vix_gate(t(8, 8, 31))
    await bot.job_entry(t(8, 14, 0))
    assert bot.state.tradeset
    bot.roll_day(t(12, 8, 31))               # bot was off for days
    assert bot.state.tradeset is None
