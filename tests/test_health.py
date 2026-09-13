"""Strategy health: full-calendar padding changes the Sharpe (the point), drawdown identities, CUSUM
divergence catches a shortfall and stays quiet on a tracking series."""

import numpy as np
import pandas as pd
import pytest

from deskboard.engine.health import assess, divergence, drawdown, full_calendar, load_history, rolling_sharpe, sharpe
from deskboard.feeds.synth import demo_pnl_history


def _daily(seed=0, n=400, mean=50.0, sd=500.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-02", periods=n)
    return pd.Series(rng.normal(mean, sd, n), index=idx)


def test_full_calendar_pads_inactive_days_with_zero_and_that_lowers_the_sharpe():
    pnl = _daily(mean=100.0)
    sparse = pnl.iloc[::5]                                  # trades one day in five
    padded = full_calendar(sparse)
    assert len(padded) == len(pnl.loc[:sparse.index[-1]]) and (padded.reindex(sparse.index) == sparse).all()
    active_only = np.sqrt(252) * sparse.mean() / sparse.std(ddof=1)
    assert sharpe(sparse) < 0.6 * active_only                # the active-day Sharpe overstates by ~sqrt(5)


def test_sharpe_needs_a_window_and_a_nonzero_std():
    assert np.isnan(sharpe(_daily(n=10)))
    assert np.isnan(sharpe(pd.Series(1.0, index=pd.bdate_range("2025-01-02", periods=40))))
    rs = rolling_sharpe(_daily(), 63)
    assert rs.iloc[:62].isna().all() and np.isfinite(rs.iloc[62:]).all()


def test_drawdown_identities():
    pnl = pd.Series([100, 100, -300, -100, 50, 400, -50], index=pd.bdate_range("2025-01-06", periods=7))
    dd = drawdown(pnl, capital=10_000.0)
    assert dd.max_dd == -400.0 and dd.max_dd_frac == pytest.approx(-0.04)
    assert dd.peak_date == pd.Timestamp("2025-01-07") and dd.trough_date == pd.Timestamp("2025-01-09")
    assert dd.current_dd == -50.0 and dd.days_in_current_dd == 1
    assert (dd.series <= 0).all() and dd.series.iloc[0] == 0.0


def test_divergence_flags_a_shortfall_and_not_a_tracking_series():
    bt = _daily(seed=1, mean=80.0, sd=400.0)
    live_ok = bt + np.random.default_rng(2).normal(0, 100.0, len(bt))          # same mean, extra noise
    d_ok = divergence(live_ok, bt)
    assert not d_ok.crossed and abs(d_ok.t_stat) < 3 and "tracking" in d_ok.reason()
    live_bad = bt - 300.0 + np.random.default_rng(4).normal(0, 100.0, len(bt))  # loses 3 diff-std per day vs backtest
    d_bad = divergence(live_bad, bt)
    assert d_bad.crossed and d_bad.crossed_on is not None and d_bad.t_stat < -10
    assert d_bad.crossed_on < bt.index[40]                                        # caught within the first two months
    assert "CUSUM crossed" in d_bad.reason()


def test_assess_rows_and_the_late_divergence_case():
    bt = _daily(seed=3, mean=60.0, sd=300.0)
    live = bt.copy()
    live.iloc[300:] -= 250.0                                                      # edge decays after day 300
    h = assess(live, backtest=bt, capital=250_000.0)
    assert h.days == 400 and np.isfinite(h.sharpe_252d)
    assert h.divergence.crossed and h.divergence.crossed_on > bt.index[300]
    assert any("live vs backtest" in r["metric"] for r in h.rows())


def test_cusum_threshold_false_flag_rate_by_simulation():
    """h = 8 keeps the false-flag rate over 500 business days under 5 %; the textbook h = 5 does not
    (measured 2.7 % vs 41 % on 2,000 paths; 300 here)."""
    rng = np.random.default_rng(5)
    idx = pd.bdate_range("2024-07-01", periods=500)
    flags = {5.0: 0, 8.0: 0}
    for _ in range(300):
        bt = pd.Series(rng.normal(40, 500, 500), index=idx)
        live = bt + rng.normal(0, 150, 500)
        for h in flags:
            flags[h] += divergence(live, bt, h=h).crossed
    assert flags[8.0] / 300 < 0.06 and flags[5.0] / 300 > 0.25


def test_demo_history_is_seeded_and_the_fade_is_caught_inside_its_window(tmp_path):
    a, b = demo_pnl_history(), demo_pnl_history()
    assert a.equals(b) and len(a) == 500 and list(a.columns) == ["date", "pnl", "backtest"]
    path = tmp_path / "h.csv"
    a.to_csv(path, index=False)
    live, bt = load_history(str(path))
    h = assess(live, bt)
    fade_start = live.index[-120]
    assert h.divergence.crossed and fade_start < h.divergence.crossed_on <= live.index[-1]
    assert h.sharpe_63d < 0 < sharpe(bt)                     # the backtest still looks fine; live does not
    (tmp_path / "bad.csv").write_text("date,x\n2025-01-02,1\n")
    with pytest.raises(ValueError):
        load_history(str(tmp_path / "bad.csv"))


def test_health_page_builds_and_appends_today(tmp_path):
    pn = pytest.importorskip("panel")  # noqa: F841
    from deskboard.engine.desk import Desk
    from deskboard.feeds.synth import demo_blotter
    from deskboard.ui.app import _health_page

    path = tmp_path / "h.csv"
    demo_pnl_history().to_csv(path, index=False)
    page = _health_page(str(path), Desk.build(demo_blotter()).book)
    assert "CUSUM crossed" in page.md.object and len(page.table.value) == 7
    assert len(page.src.data["date"]) == 500                # no marks yet: nothing appended
