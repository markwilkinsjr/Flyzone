"""
Flyzone - XSP butterfly bot. Central async loop.

Daily timeline (US Central):

    08:31  VIX gate      -> lock today's regime (Zombieland / Normal / Chaos)
    09:00  Entry (VIX >= 17): directional gate -> 0DTE single fly, limit at cap
    10:00  Cancel any unfilled single-fly order
    hh:00  Hourly GEX report 09:00-14:00 (+ structural alert, alert only)
    14:00  Entry (VIX < 17): 1DTE Batman, both wings at $0.15; once one wing
           fills, the other walks up $0.01/min to $0.20
    14:15  Time-stop recommendation; 20 % trail arms on winners
    14:45  Cancel any unfilled Batman wing (keep the filled one)
    15:00  Expiration: settle wings expiring today

Continuous:
    every 15 s  order status polling, Batman chase, cutoff cancels
    every 30 s  exit monitor (+100 % floor, trail, left-the-tent)
    always      Telegram command listener (/status /pause /resume /flatten)

Run:   python main.py            (the bot)
       python main.py --check    (verify credentials + data, no trading)
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from logging.handlers import RotatingFileHandler

import market_calendar as cal
import notifier as fmt
from config import PROJECT_ROOT, STRATEGY, TZ, RegimeParams, Settings
from gex_engine import GexReport, OptionRow, compute_gex, years_until
from indicators.supertrend import (
    SuperTrendParams,
    min_bars_required,
    swma_momentum_supertrend,
)
from models import TradeSet, Wing, d
from notifier import Notifier
from risk_manager import (
    evaluate_wing,
    realized_pnl,
    time_stop_recommendation,
    value_fly,
)
from state import BotState, StateStore
from strategy import ChainView, directional_gate, select_batman_wing, select_single_fly
from tasty_client import MarketData, OrderRouter, make_sessions

log = logging.getLogger("flyzone")
S = STRATEGY


def setup_logging(level: str) -> None:
    fmt_ = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt_)
    fh = RotatingFileHandler(PROJECT_ROOT / "flyzone.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt_)
    root.handlers[:] = [sh, fh]
    logging.getLogger("httpx").setLevel(logging.WARNING)


def at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=TZ)


class Bot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = StateStore(PROJECT_ROOT / settings.state_file
                                if not settings.state_file.is_absolute()
                                else settings.state_file)
        self.state: BotState = self.store.load()
        prefix = "[DRY] " if settings.dry_run else ("[SANDBOX] " if settings.is_sandbox else "")
        self.notify = Notifier(settings.telegram_bot_token, settings.telegram_chat_id, prefix)
        self.data: MarketData | None = None
        self.router: OrderRouter | None = None
        self._last_order_poll = datetime.min.replace(tzinfo=TZ)
        self._last_monitor = datetime.min.replace(tzinfo=TZ)
        self._last_chase: dict[int, datetime] = {}
        self._last_trail_msg: dict[int, datetime] = {}
        self._job_attempts: dict[str, int] = {}
        self._job_next_try: dict[str, datetime] = {}
        self._flatten_requested_at: datetime | None = None
        self._stop = asyncio.Event()

    # ================================================================== #
    # Lifecycle
    # ================================================================== #
    async def run(self) -> None:
        prod, order = make_sessions(self.settings)
        async with prod, order:
            self.data = MarketData(prod)
            self.data.start()
            self.router = OrderRouter(order, self.settings, self.data)
            await self.router.connect()
            self.roll_day(cal.now_ct())
            await self.startup_check()
            await self.reconcile()
            poller = asyncio.create_task(self.notify.poll_commands(self.handle_command))
            try:
                await self.loop()
            finally:
                poller.cancel()
                await self.data.stop()
                await self.notify.send("🛑 Flyzone bot stopped.")
                await self.notify.close()

    def save(self) -> None:
        self.store.save(self.state)

    async def startup_check(self) -> None:
        net_liq, cash, positions = await self.router.sanity_check()
        log.info("Account %s net liq %s cash %s positions %d",
                 self.settings.order_account_number, net_liq, cash, len(positions))
        await self.notify.send(fmt.fmt_startup(
            self.router.env_name, self.settings.dry_run,
            self.settings.order_account_number, net_liq, cash, len(positions)))

    # ================================================================== #
    # Startup reconciliation
    # ================================================================== #
    async def reconcile(self) -> None:
        """Compare state.json with what the broker says we hold."""
        ts = self.state.tradeset
        if self.settings.dry_run:
            if ts:
                await self.notify.send(f"♻️ Resumed {ts.structure} from state file (dry run).")
            return
        broker = await self.router.option_positions(S.underlying)
        broker = {k: v for k, v in broker.items() if v != 0}
        expected: dict[str, Decimal] = {}
        if ts:
            for w in ts.wings:
                if w.status in {"FILLED", "CLOSING"}:
                    for leg in w.fly.legs:
                        expected[leg.symbol] = expected.get(leg.symbol, Decimal(0)) + \
                            Decimal(leg.ratio * S.quantity)
        if broker == expected:
            if ts:
                await self.notify.send(
                    f"♻️ Reconciled: resumed {ts.structure} exp {ts.expiration}; "
                    "broker positions match the state file.")
            return

        today = cal.now_ct().date()
        if ts and not broker and date.fromisoformat(ts.expiration) < today:
            # Expired while the bot was down. Value at settlement is unknown here.
            for w in ts.wings:
                if w.is_open:
                    w.status, w.exit_reason = "EXPIRED", "EXPIRED"
                    w.notes.append("Expired while bot was offline; settlement not observed.")
            await self.notify.send("♻️ Previous trade expired while the bot was offline. "
                                   "Archived; check the broker statement for settlement.")
            self.archive_tradeset()
            return

        self.state.paused = True
        self.save()
        lines = ["⚠️ <b>Reconciliation mismatch - new entries PAUSED</b>",
                 "Broker positions:"]
        lines += [f"  {fmt.esc(k)}: {v}" for k, v in sorted(broker.items())] or ["  (none)"]
        lines.append("State file expects:")
        lines += [f"  {fmt.esc(k)}: {v}" for k, v in sorted(expected.items())] or ["  (none)"]
        lines.append("Fix manually, then send /resume.")
        await self.notify.send("\n".join(lines))

    # ================================================================== #
    # Main loop & scheduler
    # ================================================================== #
    async def loop(self) -> None:
        while not self._stop.is_set():
            now = cal.now_ct()
            try:
                self.roll_day(now)
                await self.run_scheduled_jobs(now)
                if self.state.tradeset and self.market_open(now):
                    if now - self._last_order_poll >= timedelta(seconds=S.order_poll_seconds):
                        self._last_order_poll = now
                        await self.manage_orders(now)
                    if now - self._last_monitor >= timedelta(seconds=S.monitor_interval_seconds):
                        self._last_monitor = now
                        await self.monitor_exits(now)
            except Exception as exc:
                log.exception("Main loop error")
                await self.notify.send(f"⚠️ Loop error: {fmt.esc(exc)}")
                await asyncio.sleep(10)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def market_open(self, now: datetime) -> bool:
        day = now.date()
        return (cal.is_trading_day(day)
                and S.market_open <= now.time() < cal.close_time(day))

    def roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.state.day != today:
            self.state.day = today
            self.state.vix = None
            self.state.regime = None
            self.state.plan = "Waiting for the 8:30 AM VIX gate."
            self.state.jobs_done = []
            self.state.blocked_dates = [x for x in self.state.blocked_dates if x >= today]
            self._job_attempts.clear()
            self._job_next_try.clear()
            self.expire_stale_tradeset(now.date())
            self.save()

    def expire_stale_tradeset(self, today: date) -> None:
        """A trade set whose expiration is in the past cannot still be open."""
        ts = self.state.tradeset
        if ts is None or date.fromisoformat(ts.expiration) >= today:
            return
        for w in ts.wings:
            if w.is_open:
                w.status, w.exit_reason = "EXPIRED", "EXPIRED"
                w.notes.append("Expiration passed while the bot was offline; "
                               "settlement not observed.")
            elif w.is_working:
                w.status = "CANCELLED"
        log.warning("Archived stale trade set that expired %s", ts.expiration)
        self.archive_tradeset()

    async def run_scheduled_jobs(self, now: datetime) -> None:
        day = now.date()
        if not cal.is_trading_day(day):
            return
        close = cal.close_time(day)
        jobs: list[tuple[str, datetime, datetime, object]] = [
            ("vix_gate", at(day, S.vix_gate_time), at(day, close), self.job_vix_gate),
        ]
        for h in S.gex_hours:
            start = at(day, time(h, 0))
            if start.time() < close:
                jobs.append((f"gex_{h:02d}", start, start + timedelta(minutes=50),
                             self.job_gex_report))
        regime = self.current_regime()
        if regime is not None:
            jobs.append(("entry", at(day, regime.entry_time), at(day, regime.cancel_time),
                         self.job_entry))
        jobs.append(("expiry", at(day, close) + timedelta(minutes=1),
                     at(day, time(23, 59)), self.job_expiry))

        for key, start, end, fn in jobs:
            if key in self.state.jobs_done or not (start <= now < end):
                continue
            if key == "entry" and "vix_gate" not in self.state.jobs_done:
                continue
            nxt = self._job_next_try.get(key)
            if nxt and now < nxt:
                continue
            try:
                await fn(now)
                self.state.jobs_done.append(key)
                self.save()
            except Exception as exc:
                n = self._job_attempts.get(key, 0) + 1
                self._job_attempts[key] = n
                log.exception("Job %s failed (attempt %d)", key, n)
                if n >= 3:
                    self.state.jobs_done.append(key)
                    self.save()
                    await self.notify.send(f"❌ Job <b>{key}</b> failed 3 times and was skipped: "
                                           f"{fmt.esc(exc)}")
                else:
                    self._job_next_try[key] = now + timedelta(seconds=60)

    def current_regime(self) -> RegimeParams | None:
        if self.state.vix is None:
            return None
        return S.regime_for_vix(self.state.vix)

    # ================================================================== #
    # Jobs
    # ================================================================== #
    async def job_vix_gate(self, now: datetime) -> None:
        vix = await self.data.get_index_price(S.vix_symbol)
        regime = S.regime_for_vix(vix)
        self.state.vix, self.state.regime = vix, regime.name
        blocker = self.entry_blocker(now.date())
        if blocker:
            plan = f"No new trade today: {blocker}"
        elif regime.structure == "BATMAN":
            plan = ("1-DTE Batman at 2:00 PM, wings start $0.15, walk to $0.20; "
                    "0-DTE blocked (VIX < 17).")
        else:
            plan = (f"0-DTE single directional fly at 9:00 AM gate "
                    f"({regime.wing_width:g}-pt wings, max ${regime.debit_cap}); "
                    "Batman blocked (VIX >= 17). Cancel unfilled at 10:00.")
        self.state.plan = plan
        self.save()
        await self.notify.send(fmt.fmt_vix_gate(
            vix, regime.name, regime.structure, regime.wing_width, regime.debit_cap, plan))

    def entry_blocker(self, day: date) -> str | None:
        if self.state.paused:
            return "bot is paused (/resume to re-enable)."
        if self.state.tradeset and self.state.tradeset.is_active:
            return "a position set is already open (one at a time)."
        if day.isoformat() in self.state.blocked_dates:
            return "a 1-DTE Batman expires today (no trade the following day)."
        if S.skip_early_close_days and cal.is_early_close(day):
            return "early-close session."
        return None

    async def build_report(self, expiration: date, now: datetime
                           ) -> tuple[GexReport, list[OptionRow]]:
        spot = await self.data.get_index_price(S.underlying)
        options = await self.data.options_for(S.underlying, expiration)
        if not options:
            raise RuntimeError(f"No {S.underlying} options listed for {expiration}")
        rows = await self.data.option_rows(options, spot)
        report = compute_gex(rows, spot, cal.expiration_datetime(expiration), now)
        return report, rows

    async def job_gex_report(self, now: datetime) -> None:
        ts = self.state.tradeset
        exp = (date.fromisoformat(ts.expiration) if ts and ts.has_open
               else now.date())
        report, _ = await self.build_report(exp, now)
        trade_lines: list[str] = []
        if ts and ts.has_open:
            for w in ts.wings:
                if not w.is_open:
                    continue
                fly = w.fly
                inside = fly.in_tent(report.spot)
                status = "✅ inside the tent" if inside else "↔️ outside the tent"
                trade_lines.append(f"• {fmt.esc(fly.describe())}: {status}")
                # Structural invalidation (alert only): spot beyond a long
                # strike while dealers are short gamma.
                if not inside and report.regime == "NEGATIVE":
                    trade_lines.append(
                        "  ⚠️ <b>Structural warning:</b> spot is beyond the wing in "
                        "negative gamma. Alert only; no action taken.")
        await self.notify.send(fmt.fmt_gex(report, trade_lines))

    # ------------------------------------------------------------------ #
    async def job_entry(self, now: datetime) -> None:
        regime = self.current_regime()
        blocker = self.entry_blocker(now.date())
        if blocker:
            await self.notify.send(f"⏸ Entry skipped: {fmt.esc(blocker)}")
            return
        if regime.structure == "SINGLE_FLY":
            await self.enter_single_fly(now, regime)
        else:
            await self.enter_batman(now, regime)

    async def enter_single_fly(self, now: datetime, regime: RegimeParams) -> None:
        exp = cal.expiration_for_dte(now.date(), regime.dte)
        report, rows = await self.build_report(exp, now)

        bars = await self.data.candles(S.underlying, S.candle_interval,
                                       now - timedelta(days=S.candle_lookback_days))
        params = SuperTrendParams(**S.supertrend)
        if len(bars) < min_bars_required(params):
            await self.notify.send(f"⏸ Standby: only {len(bars)} 5-min bars available; "
                                   "indicator not warmed up.")
            return
        st = swma_momentum_supertrend([b.high for b in bars], [b.low for b in bars],
                                      [b.close for b in bars], params)
        decision = directional_gate(report, st, S.require_momentum_gate)

        pick = None
        if decision.option_type:
            t = years_until(cal.expiration_datetime(exp), now)
            pick = select_single_fly(
                ChainView(rows, exp.isoformat()), decision.option_type,
                decision.target_level, report.spot, regime, t, S.pot_min)

        if decision.option_type is None:
            action = f"STANDBY (Condition {decision.condition}). {decision.reason}"
        elif pick is None or pick.fly is None:
            action = (f"Condition {decision.condition}: {decision.reason} "
                      f"ABORTED - {pick.reason if pick else ''}")
        else:
            kind = "Call" if decision.option_type == "C" else "Put"
            action = (f"Deploy <b>Single {kind} Fly</b> centred at <b>{pick.fly.center:g}</b> "
                      f"(Condition {decision.condition})\n"
                      f"• Target Spread: {fmt.esc(pick.fly.describe())}\n"
                      f"• Model mid ${pick.mid:.2f} | PoT {pick.pot:.0%} | {fmt.esc(pick.reason)}\n"
                      f"• Max Debit Limit: ${regime.debit_cap} (10% rule)")
        await self.notify.send(fmt.fmt_direction_gate(
            report.spot, self.state.vix, report, decision.trend, st.last_band,
            st.last_mom_norm, decision.gate_active, action))

        if pick is None or pick.fly is None:
            return
        ts = TradeSet(
            structure="SINGLE_FLY", regime=regime.name, opened_on=now.date().isoformat(),
            expiration=exp.isoformat(),
            wings=[Wing(fly=pick.fly, limit=str(regime.debit_cap))],
            cancel_at=at(now.date(), regime.cancel_time).isoformat(),
            condition=decision.condition,
        )
        self.state.tradeset = ts
        self.save()
        await self.submit_wing(ts, ts.wings[0], pick.mid, "Single fly")

    async def enter_batman(self, now: datetime, regime: RegimeParams) -> None:
        exp = cal.expiration_for_dte(now.date(), regime.dte)
        if S.skip_early_close_days and cal.is_early_close(exp):
            await self.notify.send("⏸ Batman skipped: expiration day is an early-close session.")
            return
        report, rows = await self.build_report(exp, now)
        chain = ChainView(rows, exp.isoformat())
        sd = report.one_sd_move or 0.0
        picks = {
            "C": select_batman_wing(chain, "C", report.call_wall, report.spot, sd, regime),
            "P": select_batman_wing(chain, "P", report.put_wall, report.spot, sd, regime),
        }
        wings = [Wing(fly=p.fly, limit=str(regime.debit_floor))
                 for p in picks.values() if p.fly is not None]
        header = (f"🦇 <b>1-DTE Batman</b> exp {exp} | spot {report.spot:.2f} | "
                  f"1-SD {sd:.2f} pts\n"
                  f"• Call wall {fmt.lvl(report.call_wall)} → "
                  f"{fmt.esc(picks['C'].fly.describe() if picks['C'].fly else picks['C'].reason)}\n"
                  f"• Put wall {fmt.lvl(report.put_wall)} → "
                  f"{fmt.esc(picks['P'].fly.describe() if picks['P'].fly else picks['P'].reason)}")
        await self.notify.send(header)
        if not wings:
            return
        ts = TradeSet(
            structure="BATMAN", regime=regime.name, opened_on=now.date().isoformat(),
            expiration=exp.isoformat(), wings=wings,
            cancel_at=at(now.date(), regime.cancel_time).isoformat(),
        )
        self.state.tradeset = ts
        self.save()
        mids = {p.fly.center: p.mid for p in picks.values() if p.fly}
        for w in wings:
            label = "Batman call wing" if w.fly.option_type == "C" else "Batman put wing"
            await self.submit_wing(ts, w, mids.get(w.fly.center), label)

    async def submit_wing(self, ts: TradeSet, wing: Wing, mid: float | None,
                          label: str) -> None:
        res = await self.router.open_fly(wing.fly, S.quantity, d(wing.limit))
        if res.order_id is None:
            wing.status = "CANCELLED"
            wing.notes.append(f"Rejected: {res.message}")
            await self.notify.send(f"❌ Order rejected ({fmt.esc(label)}): {fmt.esc(res.message)}")
        else:
            wing.order_id = res.order_id
            wing.status = "WORKING"
            await self.notify.send(fmt.fmt_order_queued(wing, label, mid, ts.cancel_at))
        self.save()
        if not ts.is_active:
            self.archive_tradeset()

    # ------------------------------------------------------------------ #
    async def job_expiry(self, now: datetime) -> None:
        """Settle wings expiring today at the closing XSP print."""
        ts = self.state.tradeset
        if not ts or ts.expiration != now.date().isoformat():
            return
        spot = self.data.latest_index_price(S.underlying) or \
            await self.data.get_index_price(S.underlying)
        for w in ts.wings:
            if w.is_working:
                w.status = "CANCELLED"
                w.notes.append("Day order expired unfilled.")
            elif w.is_open:
                value = Decimal(str(w.fly.value_at_expiry(spot))).quantize(Decimal("0.01"))
                await self.finalize_exit(w, value, "EXPIRED", now,
                                         note=f"Settled at XSP {spot:.2f} (last print).")
        self.save()
        if not ts.is_active:
            self.archive_tradeset()

    # ================================================================== #
    # Order management (fills, Batman chase, cutoff cancels, closing orders)
    # ================================================================== #
    async def manage_orders(self, now: datetime) -> None:
        ts = self.state.tradeset
        if not ts:
            return
        regime_params = {"ZOMBIELAND": S.zombieland, "NORMAL": S.normal,
                         "CHAOS": S.chaos}[ts.regime]
        cutoff = datetime.fromisoformat(ts.cancel_at)

        for w in ts.wings:
            if w.is_working and w.order_id is not None:
                res = await self.router.order_status(w.order_id)
                if res.status == "FILLED":
                    await self.on_entry_fill(ts, w, res.fill_price, regime_params, now)
                elif res.status in {"REJECTED", "CANCELLED", "EXPIRED", "REMOVED", "UNKNOWN"}:
                    w.status = "CANCELLED"
                    w.notes.append(f"Order {res.status} {res.message}")
                    await self.notify.send(f"❌ Entry order {res.status.lower()}: "
                                           f"{fmt.esc(w.fly.describe())}")
            elif w.status == "CLOSING" and w.exit_order_id is not None:
                await self.poll_closing(w, now)

        # Batman: one wing filled, the other still working -> walk it up
        if ts.structure == "BATMAN" and now < cutoff:
            filled = any(w.status == "FILLED" for w in ts.wings)
            for w in ts.wings:
                if not (filled and w.is_working and w.order_id is not None):
                    continue
                last = self._last_chase.get(id(w))
                if last and now - last < timedelta(seconds=S.chase_interval_seconds):
                    continue
                cur = d(w.limit)
                if cur >= regime_params.debit_cap:
                    continue
                new = min(cur + S.chase_step, regime_params.debit_cap)
                res = await self.router.replace_limit(w.order_id, w.fly, S.quantity, new)
                w.order_id, w.limit = res.order_id, str(new)
                self._last_chase[id(w)] = now
                log.info("Batman chase: %s limit -> %s", w.fly.describe(), new)

        # Cutoff: cancel anything still unfilled (filled wings are kept)
        if now >= cutoff:
            for w in ts.wings:
                if w.is_working and w.order_id is not None:
                    await self.router.cancel(w.order_id)
                    res = await self.router.order_status(w.order_id)
                    if res.status == "FILLED":   # filled in the race with the cancel
                        await self.on_entry_fill(ts, w, res.fill_price, regime_params, now)
                        continue
                    w.status = "CANCELLED"
                    w.notes.append("Cancelled unfilled at cutoff.")
                    await self.notify.send(
                        f"⌛ Cutoff {cutoff:%H:%M} reached: cancelled unfilled "
                        f"{fmt.esc(w.fly.describe())} (last limit {fmt.money(w.limit)}).")
        self.save()
        if not ts.is_active:
            self.archive_tradeset()

    async def on_entry_fill(self, ts: TradeSet, w: Wing, broker_fill: Decimal | None,
                            regime: RegimeParams, now: datetime) -> None:
        """
        Record the entry debit. Sandbox fills are simulated by Tastytrade and do
        not reflect real prices, so in sandbox the entry is booked at our limit
        (the worst price we accepted). In live trading the broker fill is used.
        """
        limit = d(w.limit)
        sandbox_note = None
        if self.settings.is_sandbox and not self.settings.dry_run:
            entry = limit
            sandbox_note = str(broker_fill) if broker_fill is not None else None
        elif broker_fill is not None and Decimal("0") < broker_fill <= regime.debit_cap:
            entry = broker_fill
        else:
            entry = limit
        w.status = "FILLED"
        w.entry_debit = str(entry.quantize(Decimal("0.01")))
        w.high_water = w.entry_debit
        w.filled_at = now.isoformat()
        if ts.structure == "BATMAN" and ts.expiration not in self.state.blocked_dates:
            self.state.blocked_dates.append(ts.expiration)
        label = {"C": "call", "P": "put"}[w.fly.option_type]
        await self.notify.send(fmt.fmt_fill(w, f"{ts.structure} {label}", sandbox_note))

    async def poll_closing(self, w: Wing, now: datetime) -> None:
        res = await self.router.order_status(w.exit_order_id)
        if res.status == "FILLED":
            px = res.fill_price
            if self.settings.is_sandbox or px is None:
                px = d(w.exit_price or "0")      # model price recorded at decision time
            await self.finalize_exit(w, px, w.exit_reason, now)
            return
        if res.status in {"REJECTED", "CANCELLED", "UNKNOWN"}:
            await self.notify.send(f"⚠️ Close order {res.status.lower()} for "
                                   f"{fmt.esc(w.fly.describe())}; retrying at market.")
            w.status = "FILLED"   # will be re-evaluated and re-sent next monitor tick
            return
        # Still working (limit fallback): walk the credit down.
        last = self._last_chase.get(id(w))
        if last and now - last < timedelta(seconds=S.exit_limit_step_seconds):
            return
        self._last_chase[id(w)] = now
        quotes = await self.data.leg_quotes([leg.streamer_symbol for leg in w.fly.legs])
        q = value_fly(w.fly, quotes)
        target = d(q.natural_sell or 0) - S.exit_limit_buffer
        res = await self.router.replace_close_limit(w.exit_order_id, w.fly, S.quantity, target)
        w.exit_order_id = res.order_id

    async def finalize_exit(self, w: Wing, exit_px: Decimal, reason: str | None,
                            now: datetime, note: str | None = None) -> None:
        w.status = "EXPIRED" if reason == "EXPIRED" else "CLOSED"
        w.exit_price = str(exit_px.quantize(Decimal("0.01")))
        w.exit_reason = reason
        w.closed_at = now.isoformat()
        if note:
            w.notes.append(note)
        pnl, roi = realized_pnl(d(w.entry_debit), d(w.exit_price), S.quantity,
                                S.contract_multiplier)
        ts = self.state.tradeset
        await self.notify.send(fmt.fmt_exit(w, pnl, roi, ts.structure if ts else ""))
        self.save()

    def archive_tradeset(self) -> None:
        ts = self.state.tradeset
        if ts is None:
            return
        from models import tradeset_to_dict
        self.state.history.append(tradeset_to_dict(ts))
        self.state.tradeset = None
        self.save()

    # ================================================================== #
    # Exit monitor (winners only)
    # ================================================================== #
    async def monitor_exits(self, now: datetime) -> None:
        ts = self.state.tradeset
        if not ts or not ts.has_open:
            return
        spot = self.data.latest_index_price(S.underlying) or \
            await self.data.get_index_price(S.underlying)
        recs: list[str] = []
        for w in ts.wings:
            if w.status != "FILLED":
                continue
            quotes = await self.data.leg_quotes([leg.streamer_symbol for leg in w.fly.legs])
            q = value_fly(w.fly, quotes)
            decision = evaluate_wing(w, spot, q, now)

            for ev in decision.events:
                if ev == "FLOOR_ARMED":
                    await self.notify.send(fmt.fmt_floor_armed(w, decision.value))
                elif ev in {"TRAIL_ON", "TRAIL_RAISED"}:
                    last = self._last_trail_msg.get(id(w))
                    if ev == "TRAIL_ON" or not last or now - last >= timedelta(minutes=10):
                        self._last_trail_msg[id(w)] = now
                        await self.notify.send(fmt.fmt_trail(w, decision.value, ev == "TRAIL_ON"))

            if (not ts.time_stop_sent and ts.expiration == now.date().isoformat()
                    and now.time() >= S.time_stop_recommendation):
                recs.append(f"• {fmt.esc(w.fly.describe())}\n  "
                            f"{fmt.esc(time_stop_recommendation(w, spot, decision.value))}")

            if decision.exit:
                await self.close_wing(w, decision.reason, q.natural_sell, now)

        if recs:
            ts.time_stop_sent = True
            await self.notify.send("⏰ <b>2:15 PM Time-Stop Review</b> (spot "
                                   f"{spot:.2f})\n" + "\n".join(recs) +
                                   "\nWinners now trail 20% below their peak.")
        self.save()
        if not ts.is_active:
            self.archive_tradeset()

    async def close_wing(self, w: Wing, reason: str, natural_sell: float | None,
                         now: datetime) -> None:
        model_px = Decimal(str(natural_sell or 0)).quantize(Decimal("0.01"))
        res = await self.router.close_fly_market(w.fly, S.quantity,
                                                 Decimal(str(natural_sell or 0)))
        w.exit_reason = reason
        w.exit_price = str(model_px)
        if res.order_id is None:
            await self.notify.send(f"❌ Close order rejected for {fmt.esc(w.fly.describe())}: "
                                   f"{fmt.esc(res.message)}. Will retry next tick.")
            return
        w.status = "CLOSING"
        w.exit_order_id = res.order_id
        if res.status == "FILLED":
            px = res.fill_price if (res.fill_price is not None and not self.settings.is_sandbox) \
                else model_px
            await self.finalize_exit(w, px, reason, now)

    # ================================================================== #
    # Telegram commands
    # ================================================================== #
    async def handle_command(self, cmd: str, arg: str) -> str:
        if cmd in {"/start", "/help"}:
            return ("Commands: /status, /pause, /resume, /flatten "
                    "(then /flatten CONFIRM within 60 s)")
        if cmd == "/pause":
            self.state.paused = True
            self.save()
            return "⏸ Paused: no new entries. Open positions are still managed."
        if cmd == "/resume":
            self.state.paused = False
            self.save()
            return "▶️ Resumed: new entries allowed."
        if cmd == "/status":
            return await self.status_text()
        if cmd == "/flatten":
            now = cal.now_ct()
            if arg.strip().upper() != "CONFIRM":
                self._flatten_requested_at = now
                return ("⚠️ This closes every open wing at MARKET and cancels working orders.\n"
                        "Send <b>/flatten CONFIRM</b> within 60 seconds to proceed.")
            if not self._flatten_requested_at or \
                    now - self._flatten_requested_at > timedelta(seconds=60):
                return "Confirmation expired. Send /flatten again."
            self._flatten_requested_at = None
            return await self.flatten(now)
        return "Unknown command. Try /help."

    async def status_text(self) -> str:
        ts = self.state.tradeset
        spot = self.data.latest_index_price(S.underlying) if self.data else None
        values: dict[int, Decimal | None] = {}
        if ts:
            for i, w in enumerate(ts.wings):
                quotes = await self.data.leg_quotes([leg.streamer_symbol for leg in w.fly.legs])
                q = value_fly(w.fly, quotes)
                values[i] = d(q.mid) if q.mid is not None else None
        plan = self.state.plan
        if self.state.vix is not None:
            plan = f"VIX {self.state.vix:.2f} ({self.state.regime}). {plan}"
        return fmt.fmt_status(ts, self.state.paused, plan, values, spot)

    async def flatten(self, now: datetime) -> str:
        ts = self.state.tradeset
        if not ts:
            return "Nothing to flatten."
        for w in ts.wings:
            if w.is_working and w.order_id is not None:
                await self.router.cancel(w.order_id)
                w.status = "CANCELLED"
                w.notes.append("Cancelled by /flatten.")
            elif w.status == "FILLED":
                quotes = await self.data.leg_quotes([leg.streamer_symbol for leg in w.fly.legs])
                q = value_fly(w.fly, quotes)
                await self.close_wing(w, "MANUAL_FLATTEN", q.natural_sell, now)
        self.save()
        if not ts.is_active:
            self.archive_tradeset()
        return "🧯 Flatten sent."


    # ================================================================== #
    # --check: verify credentials and data without trading
    # ================================================================== #
    async def check(self) -> None:
        prod, order = make_sessions(self.settings)
        async with prod, order:
            self.data = MarketData(prod)
            self.data.start()
            self.router = OrderRouter(order, self.settings, self.data)
            try:
                await self.router.connect()
                await self.startup_check()
                now = cal.now_ct()
                vix = await self.data.get_index_price(S.vix_symbol)
                regime = S.regime_for_vix(vix)
                print(f"VIX {vix:.2f} -> {regime.name} ({regime.structure})")
                day = now.date() if cal.is_trading_day(now.date()) else \
                    cal.next_trading_day(now.date())
                report, rows = await self.build_report(day, now)
                print(f"GEX exp {day}: spot {report.spot:.2f} zero {report.zero_gamma} "
                      f"call wall {report.call_wall} put wall {report.put_wall} "
                      f"regime {report.regime} ({len(rows)} option rows)")
                await self.notify.send(fmt.fmt_gex(report, ["(--check run, no trading)"]))
                bars = await self.data.candles(S.underlying, S.candle_interval,
                                               now - timedelta(days=S.candle_lookback_days))
                params = SuperTrendParams(**S.supertrend)
                if len(bars) >= min_bars_required(params):
                    st = swma_momentum_supertrend([b.high for b in bars], [b.low for b in bars],
                                                  [b.close for b in bars], params)
                    print(f"SuperTrend ({len(bars)} bars, last {bars[-1].time:%m-%d %H:%M}): "
                          f"{'BULLISH' if st.is_bullish else 'BEARISH'} band "
                          f"{st.last_band:.2f} mom {st.last_mom_norm:+.2f} "
                          f"gate {'ACTIVE' if st.momentum_gate_active else 'INACTIVE'}")
                else:
                    print(f"SuperTrend: only {len(bars)} bars returned")
                print("Check complete. Compare the levels and trend with your charts.")
            finally:
                await self.data.stop()
                await self.notify.close()


async def amain() -> None:
    settings = Settings.load()
    setup_logging(settings.log_level)
    bot = Bot(settings)
    if "--check" in sys.argv:
        await bot.check()
        return
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop)
        except NotImplementedError:   # Windows
            pass
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
