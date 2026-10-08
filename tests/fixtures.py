"""Synthetic XSP chain used across tests."""
from __future__ import annotations

import math

from gex_engine import OptionRow, bs_gamma
from scipy.stats import norm


def bs_price(spot, k, iv, t, typ):
    d1 = (math.log(spot / k) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if typ == "C":
        return spot * norm.cdf(d1) - k * norm.cdf(d2)
    return k * norm.cdf(-d2) - spot * norm.cdf(-d1)


def bs_delta(spot, k, iv, t, typ):
    d1 = (math.log(spot / k) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    return norm.cdf(d1) if typ == "C" else norm.cdf(d1) - 1


def make_chain(spot=675.0, t=6 / (365 * 24), iv=0.25, lo=640, hi=710,
               call_oi_peak=690, put_oi_peak=660, half_spread=0.01):
    rows = []
    for k in range(lo, hi + 1):
        for typ in ("C", "P"):
            base = 200.0
            if typ == "C" and k == call_oi_peak:
                base = 20000.0
            if typ == "P" and k == put_oi_peak:
                base = 20000.0
            px = bs_price(spot, k, iv, t, typ)
            rows.append(OptionRow(
                strike=float(k), option_type=typ, open_interest=base,
                gamma=bs_gamma(spot, k, iv, t), iv=iv,
                delta=bs_delta(spot, k, iv, t, typ),
                bid=max(px - half_spread, 0.0), ask=px + half_spread,
                symbol=f"XSP {k}{typ}", streamer_symbol=f".XSP{k}{typ}",
            ))
    return rows
