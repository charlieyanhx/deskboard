"""Black-Scholes(-Merton) price, Greeks and implied vol for European options on a
dividend-paying underlying. Per-share units; the state layer applies the multiplier.

This is the day-one pricer. The engine only needs `price`, `greeks`, `implied_vol`, so an
arbitrage-free surface pricer can replace it without touching attribution.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

YEAR = 365.0


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float      # per 1.00 change in vol (i.e. per 100 vol points)
    theta: float     # per calendar day
    rho: float


def _d1d2(S, K, T, sigma, r, q):
    v = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / v
    return d1, d1 - v


def price(S: float, K: float, T: float, sigma: float, right: str, r: float = 0.0, q: float = 0.0) -> float:
    if T <= 0:
        return max(0.0, (S - K) if right == "C" else (K - S))
    d1, d2 = _d1d2(S, K, T, sigma, r, q)
    if right == "C":
        return S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)


def greeks(S: float, K: float, T: float, sigma: float, right: str, r: float = 0.0, q: float = 0.0) -> Greeks:
    if T <= 0:
        itm = (S > K) if right == "C" else (S < K)
        d = (1.0 if right == "C" else -1.0) * (1.0 if itm else 0.0)
        return Greeks(price(S, K, T, sigma, right, r, q), d, 0.0, 0.0, 0.0, 0.0)
    d1, d2 = _d1d2(S, K, T, sigma, r, q)
    sq = np.sqrt(T)
    pdf = norm.pdf(d1)
    disc_q, disc_r = np.exp(-q * T), np.exp(-r * T)
    gamma = disc_q * pdf / (S * sigma * sq)
    vega = S * disc_q * pdf * sq
    if right == "C":
        delta = disc_q * norm.cdf(d1)
        theta = (-S * disc_q * pdf * sigma / (2 * sq) - r * K * disc_r * norm.cdf(d2) + q * S * disc_q * norm.cdf(d1)) / YEAR
        rho = K * T * disc_r * norm.cdf(d2)
    else:
        delta = -disc_q * norm.cdf(-d1)
        theta = (-S * disc_q * pdf * sigma / (2 * sq) + r * K * disc_r * norm.cdf(-d2) - q * S * disc_q * norm.cdf(-d1)) / YEAR
        rho = -K * T * disc_r * norm.cdf(-d2)
    return Greeks(price(S, K, T, sigma, right, r, q), float(delta), float(gamma), float(vega), float(theta), float(rho))


def implied_vol(target: float, S: float, K: float, T: float, right: str, r: float = 0.0, q: float = 0.0,
                lo: float = 1e-4, hi: float = 5.0) -> float:
    """Brent inversion; NaN when the price is outside no-arbitrage bounds or T <= 0."""
    if T <= 0 or not np.isfinite(target):
        return float("nan")
    intrinsic = price(S, K, T, lo, right, r, q)
    if target < intrinsic - 1e-12 or target > price(S, K, T, hi, right, r, q) + 1e-12:
        return float("nan")
    try:
        return float(brentq(lambda s: price(S, K, T, s, right, r, q) - target, lo, hi, xtol=1e-10, maxiter=200))
    except ValueError:
        return float("nan")
