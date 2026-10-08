"""
Trading-day helpers: NYSE/Cboe holidays, early closes, next expiration.

The ``holidays`` package carries the official NYSE closure list. Early closes
(1:00 PM ET) are not in that list, so the three recurring ones are derived
here. By default the bot simply does not trade on early-close days because
every time rule in the strategy assumes a 3:00 PM Central close.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import holidays

from config import TZ

_NYSE = holidays.financial_holidays("NYSE")


def is_holiday(d: date) -> bool:
    return d in _NYSE


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_trading_day(d: date) -> bool:
    return not is_weekend(d) and not is_holiday(d)


def is_early_close(d: date) -> bool:
    """Day after Thanksgiving, Christmas Eve, and July 3 (when they are trading days)."""
    if not is_trading_day(d):
        return False
    # Day after Thanksgiving: Thanksgiving is the 4th Thursday of November.
    if d.month == 11 and d.weekday() == 4:
        thursdays = [
            day for day in range(1, 31)
            if date(d.year, 11, day).weekday() == 3
        ]
        if d.day == thursdays[3] + 1:
            return True
    if d.month == 12 and d.day == 24:
        return True
    if d.month == 7 and d.day == 3:
        # July 3 closes early when Independence Day falls on a weekday.
        return date(d.year, 7, 4).weekday() < 5
    return False


def close_time(d: date) -> time:
    """Cash-session close in Central time (noon on early-close days)."""
    return time(12, 0) if is_early_close(d) else time(15, 0)


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def expiration_for_dte(today: date, dte: int) -> date:
    """0 -> today, 1 -> next trading day, and so on (XSP lists daily expirations)."""
    exp = today
    for _ in range(dte):
        exp = next_trading_day(exp)
    return exp


def expiration_datetime(exp: date) -> datetime:
    """XSP options are PM-settled: they stop trading at the cash close."""
    return datetime.combine(exp, close_time(exp), tzinfo=TZ)


def now_ct() -> datetime:
    return datetime.now(TZ)
