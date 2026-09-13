"""Strategy health from a daily P&L history: rolling Sharpe, drawdown, and whether live is
tracking the backtest.

Conventions (the ones that have burned this desk before):

* every statistic is on the FULL business-day calendar — an inactive day is $0, never
  dropped, so a strategy that trades one day in five cannot report the Sharpe of its
  active days;
* Sharpe is annualised √252 · mean / std of daily P&L (dollars, so it needs no NAV) and is
  only quoted with its window length; a window shorter than `MIN_DAYS` reports NaN;
* drawdown is on cumulative P&L from its running high-water mark, in dollars and as a
  fraction of `capital` if given;
* live-vs-backtest divergence: the live daily P&L and the backtest's expected daily P&L
  over the same dates are compared with a one-sided CUSUM on the difference (Page 1954),
  normalised by the difference's own daily std, plus a plain t-statistic on the mean
  difference. Defaults `k = 0.5`, `h = 8`: simulated on 500 business days of Gaussian noise
  the false-flag rate is 2.7 %, and a shift of 0.8 std/day is caught with a median delay of
  23 days (`h = 5`, the textbook value, flags 41 % of null paths over the same window — a
  desk would stop reading it). A crossing is a flag with the date it crossed — not a verdict
  that the edge is gone, a reason to look.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252
MIN_DAYS = 20


def load_history(path: str) -> tuple[pd.Series, pd.Series | None]:
    """CSV with columns `date`, `pnl` and optionally `backtest` (dollars per day) → (live, backtest)."""
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    if "pnl" not in df.columns:
        raise ValueError(f"{path}: need columns date, pnl[, backtest]; got {list(df.columns)}")
    return df["pnl"].astype(float), df["backtest"].astype(float) if "backtest" in df.columns else None


def full_calendar(pnl: pd.Series) -> pd.Series:
    """Pad to every business day between the first and last date; inactive days are 0."""
    s = pnl.copy()
    s.index = pd.to_datetime(s.index)
    cal = pd.bdate_range(s.index.min(), s.index.max())
    return s.groupby(s.index).sum().reindex(cal, fill_value=0.0)


def sharpe(pnl: pd.Series) -> float:
    s = full_calendar(pnl)
    if len(s) < MIN_DAYS or s.std(ddof=1) == 0:
        return float("nan")
    return float(np.sqrt(TRADING_DAYS) * s.mean() / s.std(ddof=1))


def rolling_sharpe(pnl: pd.Series, window: int = 63) -> pd.Series:
    s = full_calendar(pnl)
    r = s.rolling(window, min_periods=window)
    return np.sqrt(TRADING_DAYS) * r.mean() / r.std(ddof=1)


@dataclass(frozen=True)
class Drawdown:
    max_dd: float                # dollars, ≤ 0
    max_dd_frac: float           # of capital, NaN if capital not given
    peak_date: pd.Timestamp
    trough_date: pd.Timestamp
    current_dd: float
    days_in_current_dd: int
    series: pd.Series            # drawdown per day, dollars


def drawdown(pnl: pd.Series, capital: float | None = None) -> Drawdown:
    s = full_calendar(pnl)
    cum = s.cumsum()
    hwm = cum.cummax()
    dd = cum - hwm
    trough = dd.idxmin()
    peak = cum.loc[:trough].idxmax()
    in_dd = (dd < 0)[::-1]
    days = int(in_dd.cumprod().sum()) if len(in_dd) and in_dd.iloc[0] else 0
    return Drawdown(float(dd.min()), float(dd.min() / capital) if capital else float("nan"), peak, trough,
                    float(dd.iloc[-1]), days, dd)


@dataclass(frozen=True)
class Divergence:
    n_days: int
    mean_diff_per_day: float     # live − backtest, dollars
    t_stat: float
    cusum: pd.Series             # normalised, one-sided (live below backtest)
    threshold: float
    crossed: bool
    crossed_on: pd.Timestamp | None

    def reason(self) -> str:
        if self.crossed:
            return (f"live below backtest: CUSUM crossed {self.threshold:g} on {self.crossed_on:%Y-%m-%d}; "
                    f"mean shortfall {-self.mean_diff_per_day:,.0f}/day over {self.n_days} days (t = {self.t_stat:.2f})")
        return f"live tracking backtest: mean diff {self.mean_diff_per_day:+,.0f}/day over {self.n_days} days (t = {self.t_stat:+.2f})"


def divergence(live: pd.Series, backtest: pd.Series, h: float = 8.0, k: float = 0.5) -> Divergence:
    """One-sided CUSUM for live underperforming the backtest, on the difference normalised by its
    own daily std; `k` is the allowance (in std) and `h` the decision threshold."""
    a, b = full_calendar(live), full_calendar(backtest)
    idx = a.index.intersection(b.index)
    d = (a.loc[idx] - b.loc[idx])
    sd = float(d.std(ddof=1)) if len(idx) > 1 else float("nan")
    if not sd > 0:                            # a noiseless difference: scale by the backtest's own std instead
        sd = float(b.loc[idx].std(ddof=1)) if len(idx) > 1 else float("nan")
    z = d / sd if sd > 0 else d * 0.0
    cus = pd.Series(0.0, index=idx)
    run = 0.0
    crossed_on = None
    for t, zt in z.items():
        run = max(0.0, run - zt - k)          # accumulates when live falls short (zt negative)
        cus.loc[t] = run
        if crossed_on is None and run > h:
            crossed_on = t
    t_stat = float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))) if len(d) > 1 and d.std(ddof=1) > 0 else float("nan")
    return Divergence(len(idx), float(d.mean()), t_stat, cus, h, crossed_on is not None, crossed_on)


@dataclass(frozen=True)
class Health:
    days: int
    sharpe_full: float
    sharpe_63d: float
    sharpe_252d: float
    drawdown: Drawdown
    divergence: Divergence | None

    def rows(self) -> list[dict]:
        out = [
            {"metric": "days on the full calendar", "value": self.days},
            {"metric": "Sharpe, full window", "value": round(self.sharpe_full, 2)},
            {"metric": "Sharpe, last 63 bdays", "value": round(self.sharpe_63d, 2)},
            {"metric": "Sharpe, last 252 bdays", "value": round(self.sharpe_252d, 2)},
            {"metric": "max drawdown ($)", "value": round(self.drawdown.max_dd)},
            {"metric": "current drawdown ($, days)", "value": f"{self.drawdown.current_dd:,.0f} ({self.drawdown.days_in_current_dd})"},
        ]
        if self.divergence is not None:
            out.append({"metric": "live vs backtest", "value": self.divergence.reason()})
        return out


def assess(pnl: pd.Series, backtest: pd.Series | None = None, capital: float | None = None) -> Health:
    s = full_calendar(pnl)
    rs = rolling_sharpe(s, 63)
    ry = rolling_sharpe(s, 252)
    return Health(len(s), sharpe(s), float(rs.iloc[-1]) if len(rs) else float("nan"),
                  float(ry.iloc[-1]) if len(ry) else float("nan"), drawdown(s, capital),
                  divergence(s, backtest) if backtest is not None else None)
