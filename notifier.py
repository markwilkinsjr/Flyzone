"""
Telegram notifications and commands, using the Bot HTTP API via httpx.

Messages use Telegram's HTML parse mode (bold/italic/code render the same as
Markdown but need far less escaping). Commands are read with long polling
(``getUpdates``), and only the configured chat ID is obeyed.

Commands:
    /status            open position, value, floor state, today's plan
    /pause             stop new entries (open positions are still managed)
    /resume            allow new entries again
    /flatten           ask for confirmation
    /flatten CONFIRM   close every open wing at market and cancel working orders
"""
from __future__ import annotations

import asyncio
import html
import logging
from decimal import Decimal
from typing import Awaitable, Callable

import httpx

from gex_engine import GexReport
from models import TradeSet, Wing

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, str], Awaitable[str]]


def esc(x: object) -> str:
    return html.escape(str(x), quote=False)


def money(x: Decimal | float | str | None) -> str:
    if x is None:
        return "-"
    return f"${Decimal(str(x)):.2f}"


def lvl(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


class Notifier:
    def __init__(self, token: str, chat_id: str, prefix: str = "") -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._chat_id = str(chat_id)
        self._prefix = prefix
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(40.0))
        self._offset: int | None = None

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    async def send(self, text: str) -> None:
        """Send with retries. Failures are logged, never raised: alerts must
        not crash the trading loop."""
        body = {
            "chat_id": self._chat_id,
            "text": f"{self._prefix}{text}"[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(4):
            try:
                r = await self._client.post(f"{self._base}/sendMessage", json=body)
                if r.status_code == 200:
                    return
                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 2)
                    await asyncio.sleep(float(wait))
                    continue
                log.warning("Telegram send failed %s: %s", r.status_code, r.text[:300])
                if r.status_code == 400:
                    # Most likely an HTML parse error: resend as plain text.
                    body.pop("parse_mode", None)
            except httpx.HTTPError as exc:
                log.warning("Telegram send error: %s", exc)
            await asyncio.sleep(2 ** attempt)
        log.error("Telegram message dropped after retries: %s", text[:200])

    # ------------------------------------------------------------------ #
    async def poll_commands(self, handler: CommandHandler) -> None:
        """Long-poll for commands forever. Only the configured chat is obeyed."""
        while True:
            try:
                params = {"timeout": 30, "allowed_updates": '["message"]'}
                if self._offset is not None:
                    params["offset"] = self._offset
                r = await self._client.get(f"{self._base}/getUpdates", params=params)
                data = r.json()
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    chat = str((msg.get("chat") or {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    if chat != self._chat_id:
                        log.warning("Ignoring command from unauthorised chat %s", chat)
                        continue
                    parts = text.split(maxsplit=1)
                    cmd = parts[0].split("@")[0].lower()
                    arg = parts[1] if len(parts) > 1 else ""
                    try:
                        reply = await handler(cmd, arg)
                    except Exception as exc:  # never let a command kill the poller
                        log.exception("Command %s failed", cmd)
                        reply = f"⚠️ Command failed: {esc(exc)}"
                    if reply:
                        await self.send(reply)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(5)


# --------------------------------------------------------------------------- #
# Message formatters
# --------------------------------------------------------------------------- #
def fmt_startup(env: str, dry_run: bool, account: str, net_liq: Decimal | None,
                cash: Decimal | None, positions: int) -> str:
    mode = "DRY RUN (no orders sent)" if dry_run else "LIVE ORDERS"
    return (
        "🤖 <b>Flyzone bot online</b>\n"
        f"• Order environment: <b>{esc(env)}</b>\n"
        f"• Mode: <b>{mode}</b>\n"
        f"• Account: <code>{esc(account)}</code>\n"
        f"• Net liq: {money(net_liq)} | Cash: {money(cash)}\n"
        f"• Open positions at broker: {positions}"
    )


def fmt_vix_gate(vix: float, regime: str, structure: str, width: Decimal,
                 cap: Decimal, plan: str) -> str:
    return (
        "🌡 <b>VIX Volatility Gate</b>\n"
        f"• VIX: <b>{vix:.2f}</b> → <b>{esc(regime)}</b>\n"
        f"• Structure: {esc(structure)} | Wings: {width:g} pt | Max debit: {money(cap)}\n"
        f"• Plan: {esc(plan)}"
    )


def fmt_gex(report: GexReport, trade_lines: list[str]) -> str:
    regime_icon = "🟢" if report.regime == "POSITIVE" else "🔴"
    side = report.above_zero_gamma
    side_txt = "n/a" if side is None else ("above" if side else "below")
    lines = [
        f"📊 <b>XSP GEX Update</b> ({report.timestamp:%H:%M} CT, exp {esc(report.expiration)})",
        "<pre>",
        f"Spot        {report.spot:>9.2f}",
        f"Zero Gamma  {lvl(report.zero_gamma):>9}  (spot {side_txt})",
        f"Call Wall   {lvl(report.call_wall):>9}",
        f"Put Wall    {lvl(report.put_wall):>9}",
        f"Net GEX     {report.total_gex/1e6:>8.1f}M",
        f"1-SD move   {lvl(report.one_sd_move):>9}",
        "</pre>",
        f"{regime_icon} Regime: <b>{report.regime} GAMMA</b> "
        + ("(mean-reverting, compression between walls)" if report.regime == "POSITIVE"
           else "(volatility expansion, directional risk)"),
    ]
    if trade_lines:
        lines.append("")
        lines.extend(trade_lines)
    return "\n".join(lines)


def fmt_direction_gate(spot: float, vix: float, report: GexReport, trend: str,
                       band: float, mom_norm: float, gate_active: bool,
                       action: str) -> str:
    icon = "🟢" if trend == "BULLISH" else "🔴"
    return (
        "🤖 <b>XSP Directional Gate Check</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Spot:</b> {spot:.2f} | <b>VIX:</b> {vix:.2f}\n"
        f"• <b>Zero Gamma Flip:</b> {lvl(report.zero_gamma)} (Regime: "
        f"{'+' if report.regime == 'POSITIVE' else '-'}Gamma)\n"
        f"• <b>Call Wall:</b> {lvl(report.call_wall)} | <b>Put Wall:</b> {lvl(report.put_wall)}\n\n"
        "📊 <b>RQ SWMA Momentum SuperTrend (5m):</b>\n"
        f"• <b>Trend:</b> {icon} {trend} (ST Band: {band:.2f})\n"
        f"• <b>Normalized Mom:</b> {mom_norm:+.2f} "
        f"(Gated: {'ACTIVE' if gate_active else 'INACTIVE'})\n\n"
        f"🎯 <b>Action:</b> {action}"
    )


def fmt_order_queued(wing: Wing, label: str, mid: float | None, cancel_at: str) -> str:
    return (
        f"📝 <b>Order queued</b> ({esc(label)})\n"
        f"• {esc(wing.fly.describe())}\n"
        f"• Limit debit: {money(wing.limit)} | Model mid: {money(mid)}\n"
        f"• Cancels if unfilled at {esc(cancel_at[11:16])} CT"
    )


def fmt_fill(wing: Wing, label: str, sandbox_fill: str | None = None) -> str:
    extra = (f"\n• Broker-reported fill: {money(sandbox_fill)} (sandbox fills are simulated)"
             if sandbox_fill else "")
    return (
        f"✅ <b>Filled</b> ({esc(label)})\n"
        f"• {esc(wing.fly.describe())}\n"
        f"• Net debit paid: <b>{money(wing.entry_debit)}</b> "
        f"(${Decimal(wing.entry_debit) * 100:.2f} per contract){extra}"
    )


def fmt_floor_armed(wing: Wing, value: Decimal) -> str:
    return (
        "🔒 <b>+100% Profit Floor ARMED</b>\n"
        f"• {esc(wing.fly.describe())}\n"
        f"• Entry {money(wing.entry_debit)} → now {money(value)}\n"
        f"• Floor locked at {money(wing.floor)}. It never moves down."
    )


def fmt_trail(wing: Wing, value: Decimal, new: bool) -> str:
    head = "📈 <b>2:15 Trail ACTIVATED</b>" if new else "📈 <b>Trail raised</b>"
    return (f"{head}\n• {esc(wing.fly.describe())}\n"
            f"• Value {money(value)} | Peak {money(wing.high_water)} | Floor {money(wing.floor)}")


def fmt_exit(wing: Wing, pnl: Decimal, roi: Decimal, label: str) -> str:
    icon = "💰" if pnl > 0 else "🧾"
    reason = {
        "PROFIT_FLOOR": "Trailing Lock (+100% floor)",
        "TRAILING_LOCK": "Trailing Lock (2:15 trail)",
        "LEFT_TENT": "Price left the tent while profitable",
        "EXPIRED": "Expired / settled",
        "MANUAL_FLATTEN": "Manual /flatten",
    }.get(wing.exit_reason or "", wing.exit_reason or "-")
    return (
        f"{icon} <b>Trade Exit</b> ({esc(label)})\n"
        f"• {esc(wing.fly.describe())}\n"
        f"• Entry {money(wing.entry_debit)} → Exit {money(wing.exit_price)}\n"
        f"• P&amp;L: <b>{'+' if pnl >= 0 else ''}${pnl:.2f}</b> ({roi * 100:+.0f}%)\n"
        f"• Reason: {esc(reason)}"
    )


def fmt_status(ts: TradeSet | None, paused: bool, plan: str,
               values: dict[int, Decimal | None], spot: float | None) -> str:
    lines = [f"📋 <b>Status</b> {'(PAUSED)' if paused else ''}",
             f"• Today: {esc(plan)}",
             f"• Spot: {lvl(spot)}"]
    if ts is None:
        lines.append("• No active trade.")
        return "\n".join(lines)
    lines.append(f"• {esc(ts.structure)} ({esc(ts.regime)}) exp {esc(ts.expiration)}")
    for i, w in enumerate(ts.wings):
        lines.append(
            f"  {i + 1}. {esc(w.fly.describe())}\n"
            f"     {w.status} | entry {money(w.entry_debit)} | now {money(values.get(i))} | "
            f"peak {money(w.high_water)} | floor {money(w.floor)}"
            f"{' (armed)' if w.floor_armed else ''}{' (trail)' if w.trail_active else ''}"
        )
    return "\n".join(lines)
