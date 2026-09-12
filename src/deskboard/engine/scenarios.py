"""Scenario ladder: full revaluation of the book on a spot × vol grid from the current marks.

For every leg with a valid mark (mid, implied vol, spot, time to expiry) the price under a
scenario is `bs.price(S·(1+ds), K, T − dt, σ + dσ, right)`; stock legs move linearly. The
ladder reports P&L versus the model price at the current mark (the Black-Scholes price at
the mark's implied vol, which equals the mid to the solver's tolerance), in dollars, so the
(0, 0) cell is exactly 0 by construction and the zero-shock row and column reproduce the
book's dollar Greeks to first order — both are tests.
Legs without a valid mark contribute nothing and are listed in `missing`, never silently
priced at zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import greeks as bs
from .book import Book, years_to_expiry

DEFAULT_SPOT = (-0.10, -0.05, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.05, 0.10)
DEFAULT_VOL = (-0.05, -0.02, 0.0, 0.02, 0.05, 0.10)


@dataclass(frozen=True)
class Ladder:
    spot_shocks: tuple[float, ...]
    vol_shocks: tuple[float, ...]
    pnl: np.ndarray                     # shape (len(vol_shocks), len(spot_shocks)), dollars vs current mark
    days: float
    missing: tuple[str, ...] = field(default_factory=tuple)

    def worst(self) -> tuple[float, float, float]:
        i, j = np.unravel_index(np.argmin(self.pnl), self.pnl.shape)
        return float(self.pnl[i, j]), self.spot_shocks[j], self.vol_shocks[i]

    def to_rows(self) -> list[dict]:
        col = lambda ds: f"{ds:+.0%}" if ds else "0"  # noqa: E731
        return [{"vol_shock": dv, **{col(ds): float(round(self.pnl[i, j])) for j, ds in enumerate(self.spot_shocks)}}
                for i, dv in enumerate(self.vol_shocks)]


def _leg_value(ls, ds: float, dv: float, days: float, r: float, q: float) -> float:
    m = ls.mark
    scale = ls.sq * ls.mult
    if ls.leg.get("sec_type", "OPT") == "STK":
        return m.mid * (1 + ds) * scale
    K = float(ls.leg["strike"])
    T = max(years_to_expiry(ls.leg["expiration"], m.ts) - days / 365.0, 0.0)
    return bs.price(m.spot * (1 + ds), K, T, max(m.iv + dv, 1e-4), ls.leg["right"], r, q) * scale


def ladder(book: Book, spot_shocks=DEFAULT_SPOT, vol_shocks=DEFAULT_VOL, days: float = 0.0) -> Ladder:
    """P&L of the whole book on the grid, relative to the current marks. `days` rolls time
    forward (theta) before repricing; 0 gives an instantaneous shock."""
    spot_shocks, vol_shocks = tuple(spot_shocks), tuple(vol_shocks)
    pnl = np.zeros((len(vol_shocks), len(spot_shocks)))
    missing = []
    for pid, legs in book.legs.items():
        for ls in legs:
            m = ls.mark
            is_stock = ls.leg.get("sec_type", "OPT") == "STK"
            if m is None or (not is_stock and (m.g is None or not np.isfinite(m.iv))):
                missing.append(f"{pid}:{ls.key}")
                continue
            base = _leg_value(ls, 0.0, 0.0, 0.0, book.r, book.q)
            for i, dv in enumerate(vol_shocks):
                for j, ds in enumerate(spot_shocks):
                    pnl[i, j] += _leg_value(ls, ds, dv, days, book.r, book.q) - base
    return Ladder(spot_shocks, vol_shocks, pnl, days, tuple(missing))
