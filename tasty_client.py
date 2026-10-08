"""
Tastytrade API wrappers.

Two sessions are used and kept strictly separate:

* ``MarketData``  - PRODUCTION session, read-only. Option chains, Greeks,
                    open interest, quotes, index prices and 5-minute candles.
                    The sandbox has no usable market data, so levels must come
                    from production. This class has no order methods at all.
* ``OrderRouter`` - SANDBOX session (``is_test=True``). Every order passes
                    through ``_guard()``, which refuses to dispatch unless the
                    session really is a sandbox session (or live trading was
                    explicitly enabled in config). In DRY_RUN mode no request
                    is sent: fills are simulated from live quotes.

Session tokens are refreshed automatically by the SDK before every request
(``Session.refresh`` with a 60-second buffer).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

from tastytrade import Account, DXLinkStreamer, Session
from tastytrade.dxfeed import Candle, Greeks, Quote, Summary, Trade
from tastytrade.instruments import Option, get_option_chain
from tastytrade.order import (
    InstrumentType,
    Leg,
    LimitOrder,
    MarketOrder,
    OrderAction,
    OrderStatus,
    OrderTimeInForce,
)

from config import TZ, Settings
from gex_engine import OptionRow
from models import Fly

log = logging.getLogger(__name__)

TICK = Decimal("0.01")


def _f(x) -> float | None:
    """Decimal/float/None -> float or None (NaN treated as missing)."""
    if x is None:
        return None
    v = float(x)
    return None if math.isnan(v) else v


# --------------------------------------------------------------------------- #
# Streaming quote cache (production feed)
# --------------------------------------------------------------------------- #
class QuoteCache:
    """
    One long-lived DXLink connection that keeps the latest Quote, Greeks,
    Summary and Trade event per symbol. Reconnects with back-off and
    re-subscribes everything after a drop.
    """

    TYPES = (Quote, Greeks, Summary, Trade)

    def __init__(self, session: Session) -> None:
        self._session = session
        self._streamer: DXLinkStreamer | None = None
        self._subs: dict[type, set[str]] = {t: set() for t in self.TYPES}
        self._data: dict[type, dict[str, object]] = {t: {} for t in self.TYPES}
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="quote-cache")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        backoff = 1
        while True:
            try:
                async with DXLinkStreamer(self._session) as streamer:
                    self._streamer = streamer
                    for t, syms in self._subs.items():
                        if syms:
                            await streamer.subscribe(t, sorted(syms))
                    self._ready.set()
                    backoff = 1
                    log.info("DXLink market-data stream connected")
                    await asyncio.gather(*(self._consume(streamer, t) for t in self.TYPES))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("DXLink stream dropped (%s); reconnecting in %ss", exc, backoff)
            finally:
                self._ready.clear()
                self._streamer = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _consume(self, streamer: DXLinkStreamer, event_type: type) -> None:
        store = self._data[event_type]
        async for ev in streamer.listen(event_type):
            store[ev.event_symbol] = ev

    async def subscribe(self, event_type: type, symbols: list[str]) -> None:
        new = [s for s in symbols if s not in self._subs[event_type]]
        if not new:
            return
        self._subs[event_type].update(new)
        await asyncio.wait_for(self._ready.wait(), timeout=30)
        async with self._lock:
            if self._streamer is not None:
                await self._streamer.subscribe(event_type, new)

    def get(self, event_type: type, symbol: str):
        return self._data[event_type].get(symbol)

    def bid_ask(self, symbol: str) -> tuple[float | None, float | None]:
        q = self.get(Quote, symbol)
        if q is None:
            return (None, None)
        return (_f(q.bid_price), _f(q.ask_price))


# --------------------------------------------------------------------------- #
# Market data (PRODUCTION, read-only)
# --------------------------------------------------------------------------- #
@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float


class MarketData:
    def __init__(self, session: Session) -> None:
        if session.is_test:
            raise RuntimeError("MarketData expects the PRODUCTION session (sandbox has no data).")
        self.session = session
        self.cache = QuoteCache(session)
        self._chain: dict[date, list[Option]] = {}
        self._chain_loaded: datetime | None = None

    def start(self) -> None:
        self.cache.start()

    async def stop(self) -> None:
        await self.cache.stop()

    # ---- index prices ---------------------------------------------------- #
    async def get_index_price(self, symbol: str, timeout: float = 15.0) -> float:
        """Last trade for an index (XSP, VIX); falls back to quote mid."""
        await self.cache.subscribe(Trade, [symbol])
        await self.cache.subscribe(Quote, [symbol])
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            t = self.cache.get(Trade, symbol)
            if t is not None and _f(t.price):
                return float(t.price)
            bid, ask = self.cache.bid_ask(symbol)
            if bid and ask:
                return (bid + ask) / 2
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"No price for {symbol} within {timeout}s")
            await asyncio.sleep(0.25)

    def latest_index_price(self, symbol: str) -> float | None:
        t = self.cache.get(Trade, symbol)
        if t is not None and _f(t.price):
            return float(t.price)
        bid, ask = self.cache.bid_ask(symbol)
        return (bid + ask) / 2 if bid and ask else None

    # ---- option chain ---------------------------------------------------- #
    async def options_for(self, underlying: str, expiration: date) -> list[Option]:
        stale = (self._chain_loaded is None
                 or datetime.now(TZ) - self._chain_loaded > timedelta(hours=1))
        if stale or expiration not in self._chain:
            self._chain = await get_option_chain(self.session, underlying)
            self._chain_loaded = datetime.now(TZ)
        opts = self._chain.get(expiration, [])
        # XSP can list AM- and PM-settled contracts on the same date; trade PM.
        pm = [o for o in opts if (o.settlement_type or "").upper().startswith("PM")]
        return pm or opts

    async def option_rows(self, options: list[Option], spot: float,
                          band_pct: float = 0.05, wait: float = 10.0,
                          coverage: float = 0.9) -> list[OptionRow]:
        """
        Snapshot quotes, Greeks and open interest for every strike within
        ``band_pct`` of spot. Waits until ``coverage`` of the symbols have
        Greeks + quotes, or ``wait`` seconds pass.
        """
        lo, hi = spot * (1 - band_pct), spot * (1 + band_pct)
        sel = [o for o in options if lo <= float(o.strike_price) <= hi]
        syms = [o.streamer_symbol for o in sel]
        for t in (Quote, Greeks, Summary):
            await self.cache.subscribe(t, syms)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while loop.time() < deadline:
            have = sum(1 for s in syms
                       if self.cache.get(Greeks, s) is not None
                       and self.cache.get(Quote, s) is not None)
            if syms and have / len(syms) >= coverage:
                break
            await asyncio.sleep(0.5)

        rows: list[OptionRow] = []
        for o in sel:
            g = self.cache.get(Greeks, o.streamer_symbol)
            s = self.cache.get(Summary, o.streamer_symbol)
            bid, ask = self.cache.bid_ask(o.streamer_symbol)
            rows.append(OptionRow(
                strike=float(o.strike_price),
                option_type=o.option_type.value,
                open_interest=float(s.open_interest) if s and s.open_interest else 0.0,
                gamma=_f(g.gamma) if g else None,
                iv=_f(g.volatility) if g else None,
                delta=_f(g.delta) if g else None,
                bid=bid, ask=ask,
                symbol=o.symbol,
                streamer_symbol=o.streamer_symbol,
            ))
        missing_oi = sum(1 for r in rows if r.open_interest == 0)
        log.info("Chain snapshot: %d rows, %d without OI", len(rows), missing_oi)
        return rows

    async def leg_quotes(self, streamer_symbols: list[str]) -> dict[str, tuple]:
        await self.cache.subscribe(Quote, streamer_symbols)
        return {s: self.cache.bid_ask(s) for s in streamer_symbols}

    # ---- candles --------------------------------------------------------- #
    async def candles(self, symbol: str, interval: str, start: datetime,
                      quiet: float = 2.0, timeout: float = 20.0) -> list[Bar]:
        """
        Historical + current candles for ``symbol`` (regular trading hours).
        Collects events until the feed goes quiet. Only CLOSED bars are
        returned so the indicator matches Pine's confirmed-bar values.
        """
        bars: dict[int, Bar] = {}
        loop = asyncio.get_running_loop()
        async with DXLinkStreamer(self.session) as streamer:
            await streamer.subscribe_candle([symbol], interval, start_time=start)
            deadline = loop.time() + timeout
            last_event = loop.time()
            while loop.time() < deadline:
                try:
                    ev = await asyncio.wait_for(streamer.get_event(Candle), timeout=quiet)
                except asyncio.TimeoutError:
                    if bars and loop.time() - last_event >= quiet:
                        break
                    continue
                last_event = loop.time()
                vals = [_f(ev.open), _f(ev.high), _f(ev.low), _f(ev.close)]
                if any(v is None for v in vals) or not ev.time:
                    continue
                bars[ev.time] = Bar(
                    datetime.fromtimestamp(ev.time / 1000, tz=timezone.utc).astimezone(TZ),
                    *vals)
        minutes = int(interval.rstrip("m"))
        now = datetime.now(TZ)
        closed = [b for b in bars.values() if b.time + timedelta(minutes=minutes) <= now]
        return sorted(closed, key=lambda b: b.time)


# --------------------------------------------------------------------------- #
# Order routing (SANDBOX)
# --------------------------------------------------------------------------- #
@dataclass
class OrderResult:
    order_id: int | None
    status: str                    # LIVE, FILLED, CANCELLED, REJECTED, ...
    fill_price: Decimal | None = None
    message: str = ""


class OrderRouter:
    """
    Builds and routes butterfly orders.

    Multi-leg construction: a butterfly is ONE order with three legs, so all
    legs fill together or not at all (no legging risk):

        call fly:  BUY_TO_OPEN 1 x lower C, SELL_TO_OPEN 2 x centre C,
                   BUY_TO_OPEN 1 x upper C
        put fly:   same with puts.

    The order price is the NET price of the whole package. In the Tastytrade
    API a negative price is a debit (we pay), positive is a credit. A Batman is
    two separate 3-leg orders (one per wing) because each wing is managed and
    filled independently.
    """

    def __init__(self, session: Session, settings: Settings, data: MarketData) -> None:
        self.session = session
        self.settings = settings
        self.data = data
        self.account: Account | None = None
        self._sim_ids = itertools.count(900_000_000)
        self._sim_orders: dict[int, dict] = {}

    # ---- safety ---------------------------------------------------------- #
    def _guard(self) -> None:
        """Refuse to dispatch an order unless the session matches the config."""
        if self.session.is_test != self.settings.is_sandbox:
            raise RuntimeError(
                "SAFETY STOP: order session environment does not match "
                "TASTYTRADE_IS_SANDBOX. No order was sent."
            )

    @property
    def env_name(self) -> str:
        return "SANDBOX (cert)" if self.session.is_test else "PRODUCTION"

    # ---- account --------------------------------------------------------- #
    async def connect(self) -> None:
        self.account = await Account.get(self.session, self.settings.order_account_number)

    async def sanity_check(self) -> tuple[Decimal | None, Decimal | None, list]:
        bal = await self.account.get_balances(self.session)
        positions = await self.account.get_positions(self.session)
        return bal.net_liquidating_value, bal.cash_balance, positions

    async def option_positions(self, underlying: str) -> dict[str, Decimal]:
        """Signed quantity per OCC symbol (long +, short -)."""
        positions = await self.account.get_positions(self.session, underlying_symbols=[underlying])
        out: dict[str, Decimal] = {}
        for p in positions:
            if p.instrument_type != InstrumentType.EQUITY_OPTION:
                continue
            sign = -1 if p.quantity_direction.lower() == "short" else 1
            out[p.symbol] = Decimal(p.quantity) * sign
        return out

    # ---- order construction --------------------------------------------- #
    @staticmethod
    def _legs(fly: Fly, qty: int, opening: bool) -> list[Leg]:
        legs = []
        for leg in fly.legs:
            long_leg = leg.ratio > 0
            if opening:
                action = OrderAction.BUY_TO_OPEN if long_leg else OrderAction.SELL_TO_OPEN
            else:
                action = OrderAction.SELL_TO_CLOSE if long_leg else OrderAction.BUY_TO_CLOSE
            legs.append(Leg(
                instrument_type=InstrumentType.EQUITY_OPTION,
                symbol=leg.symbol,
                action=action,
                quantity=Decimal(abs(leg.ratio) * qty),
            ))
        return legs

    # ---- open ------------------------------------------------------------ #
    async def open_fly(self, fly: Fly, qty: int, limit_debit: Decimal) -> OrderResult:
        """Day limit order to BUY the fly for at most ``limit_debit`` per spread."""
        limit_debit = limit_debit.quantize(TICK, rounding=ROUND_DOWN)
        if self.settings.dry_run:
            oid = next(self._sim_ids)
            self._sim_orders[oid] = {"fly": fly, "limit": limit_debit, "status": "LIVE",
                                     "side": "open"}
            log.info("[DRY RUN] open %s @ %s debit (sim id %s)", fly.describe(), limit_debit, oid)
            return OrderResult(oid, "LIVE", message="simulated")
        self._guard()
        order = LimitOrder(
            time_in_force=OrderTimeInForce.DAY,
            legs=self._legs(fly, qty, opening=True),
            price=-limit_debit,               # negative = debit
        )
        resp = await self.account.place_order(self.session, order, dry_run=False)
        if resp.errors:
            return OrderResult(None, "REJECTED", message=str(resp.errors))
        return OrderResult(resp.order.id, resp.order.status.value.upper())

    async def replace_limit(self, order_id: int, fly: Fly, qty: int,
                            new_debit: Decimal) -> OrderResult:
        new_debit = new_debit.quantize(TICK, rounding=ROUND_DOWN)
        if self.settings.dry_run:
            self._sim_orders[order_id]["limit"] = new_debit
            return OrderResult(order_id, "LIVE", message="simulated")
        self._guard()
        order = LimitOrder(time_in_force=OrderTimeInForce.DAY,
                           legs=self._legs(fly, qty, opening=True), price=-new_debit)
        placed = await self.account.replace_order(self.session, order_id, order)
        return OrderResult(placed.id, placed.status.value.upper())

    async def cancel(self, order_id: int) -> None:
        if self.settings.dry_run:
            if order_id in self._sim_orders:
                self._sim_orders[order_id]["status"] = "CANCELLED"
            return
        self._guard()
        try:
            await self.account.delete_order(self.session, order_id)
        except Exception as exc:
            log.warning("Cancel %s failed (may already be filled/cancelled): %s", order_id, exc)

    async def order_status(self, order_id: int) -> OrderResult:
        """Poll an order. In DRY_RUN, simulate a fill against live quotes."""
        if self.settings.dry_run:
            return await self._sim_status(order_id)
        placed = await self.account.get_order(self.session, order_id)
        status = placed.status
        fill = None
        if status == OrderStatus.FILLED:
            fill = self._net_fill_price(placed)
        return OrderResult(order_id, status.value.upper(), fill, placed.reject_reason or "")

    @staticmethod
    def _net_fill_price(placed) -> Decimal | None:
        """
        Net per-spread price from leg fills (positive = debit for an opening
        buy, credit for a closing sell). Spreads = smallest leg quantity,
        because the long wings are 1 lot per spread and the body is 2.
        """
        if not placed.legs or any(not leg.fills for leg in placed.legs):
            return abs(placed.price) if placed.price is not None else None
        cash = Decimal("0")
        spreads = None
        for leg in placed.legs:
            qty = sum(Decimal(f.quantity) for f in leg.fills)
            notional = sum(Decimal(f.fill_price) * Decimal(f.quantity) for f in leg.fills)
            cash += notional if leg.action.value.startswith("Buy") else -notional
            spreads = qty if spreads is None else min(spreads, qty)
        if not spreads:
            return None
        return (abs(cash) / spreads).quantize(Decimal("0.0001"))

    # ---- close ----------------------------------------------------------- #
    async def close_fly_market(self, fly: Fly, qty: int,
                               fallback_credit: Decimal | None) -> OrderResult:
        """
        Sell-to-close the whole fly at MARKET (per the spec). If the broker
        rejects a multi-leg market order, fall back to a marketable limit at
        the natural bid-side credit and walk it down until filled.
        """
        if self.settings.dry_run:
            oid = next(self._sim_ids)
            quotes = await self.data.leg_quotes([leg.streamer_symbol for leg in fly.legs])
            from risk_manager import value_fly
            q = value_fly(fly, quotes)
            px = Decimal(str(q.natural_sell or 0)).quantize(TICK)
            self._sim_orders[oid] = {"status": "FILLED", "fill": px, "side": "close"}
            log.info("[DRY RUN] market close %s @ %s", fly.describe(), px)
            return OrderResult(oid, "FILLED", px, "simulated")
        self._guard()
        order = MarketOrder(time_in_force=OrderTimeInForce.DAY,
                            legs=self._legs(fly, qty, opening=False))
        try:
            resp = await self.account.place_order(self.session, order, dry_run=False)
            if not resp.errors:
                return OrderResult(resp.order.id, resp.order.status.value.upper())
            log.warning("Market close rejected: %s", resp.errors)
        except Exception as exc:
            log.warning("Market close rejected (%s); using marketable limit", exc)
        credit = max((fallback_credit or Decimal("0.05")).quantize(TICK), Decimal("0.01"))
        order = LimitOrder(time_in_force=OrderTimeInForce.DAY,
                           legs=self._legs(fly, qty, opening=False), price=credit)
        resp = await self.account.place_order(self.session, order, dry_run=False)
        if resp.errors:
            return OrderResult(None, "REJECTED", message=str(resp.errors))
        return OrderResult(resp.order.id, resp.order.status.value.upper(),
                           message=f"limit fallback @ {credit}")

    async def replace_close_limit(self, order_id: int, fly: Fly, qty: int,
                                  credit: Decimal) -> OrderResult:
        """Walk a fallback closing limit down (credit order, positive price)."""
        credit = max(credit.quantize(TICK, rounding=ROUND_DOWN), Decimal("0.01"))
        if self.settings.dry_run:
            return OrderResult(order_id, "FILLED", credit, "simulated")
        self._guard()
        order = LimitOrder(time_in_force=OrderTimeInForce.DAY,
                           legs=self._legs(fly, qty, opening=False), price=credit)
        placed = await self.account.replace_order(self.session, order_id, order)
        return OrderResult(placed.id, placed.status.value.upper(),
                           message=f"limit fallback @ {credit}")

    # ---- dry-run simulation --------------------------------------------- #
    async def _sim_status(self, order_id: int) -> OrderResult:
        o = self._sim_orders.get(order_id)
        if o is None:
            return OrderResult(order_id, "UNKNOWN")
        if o["status"] != "LIVE":
            return OrderResult(order_id, o["status"], o.get("fill"))
        fly: Fly = o["fly"]
        quotes = await self.data.leg_quotes([leg.streamer_symbol for leg in fly.legs])
        from risk_manager import value_fly
        q = value_fly(fly, quotes)
        # Simulated fill rule: fills when the natural (ask-side) debit is at or
        # below our limit, at that natural price.
        if q.natural_buy is not None and Decimal(str(q.natural_buy)) <= o["limit"]:
            o["status"] = "FILLED"
            o["fill"] = Decimal(str(q.natural_buy)).quantize(TICK)
        return OrderResult(order_id, o["status"], o.get("fill"))


# --------------------------------------------------------------------------- #
# Session factory
# --------------------------------------------------------------------------- #
def make_sessions(settings: Settings) -> tuple[Session, Session]:
    """(production read-only data session, order session).

    The order session is the sandbox (``is_test=True``) unless live trading
    was explicitly enabled, in which case it uses the production credentials.
    """
    prod = Session(settings.prod_client_secret, settings.prod_refresh_token, is_test=False)
    if settings.is_sandbox:
        order = Session(settings.sandbox_client_secret, settings.sandbox_refresh_token,
                        is_test=True)
    else:
        order = Session(settings.prod_client_secret, settings.prod_refresh_token,
                        is_test=False)
    return prod, order
