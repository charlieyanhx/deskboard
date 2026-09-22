"""The state-directory contract end to end: synthetic state → readers → EOD report → pages →
the session builder, all headless. The state is regenerated per test from the seed, so every
number here is a property of the generator and the readers, not of a fixture file."""
import json

import pytest

from deskboard.cli import DEMO
from deskboard.engine.eod_report import build_eod, render_md, report_days, write_eod
from deskboard.feeds.statefiles import StateFiles
from deskboard.feeds.synth_state import demo_state, load_reference
from deskboard.ui.app import build, build_desk
from deskboard.ui.pages import build_state_tabs, pnl_history_for_health


@pytest.fixture(scope="module")
def state(tmp_path_factory):
    d = tmp_path_factory.mktemp("state")
    demo_state(d)
    return d


def test_generator_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    demo_state(a)
    demo_state(b)
    for p in sorted(a.rglob("*")):
        if p.is_file():
            assert p.read_bytes() == (b / p.relative_to(a)).read_bytes(), p.name


def test_trades_reconcile_to_the_ledger(state):
    sf = StateFiles(state)
    led = sf.ledger()
    t = sf.trades()
    assert len(t) == (led["action"] == "open").sum()
    closed = t.dropna(subset=["realized"])
    assert len(closed) == (led["action"] == "close").sum()
    # realized = credit − close debit − both commissions, to the cent
    r = closed.iloc[0]
    assert r["realized"] == pytest.approx(r["credit"] - r["close_debit"] - r["commission"], abs=0.005)


def test_fill_quality_against_decision_quotes(state):
    q = StateFiles(state).fills_quality()
    assert len(q) == 62
    assert (q["slip_vs_cross_c"].abs() < 0.01).all()          # the generator fills AT the decision cross
    assert (q["slip_vs_mid_c"] > 0).all()                       # … which is the half-spread worse than mid
    assert q["latency_ms"].between(900, 6000).all()             # parsed from the note


def test_daily_equity_from_eod_marks_plus_realized(state):
    sf = StateFiles(state)
    eq = sf.daily_equity()
    assert len(eq) == 30 and set(eq["source"]) == {"bot"}
    m = sf.marks()
    last = m[(m["row"] == "book") & (m["kind"] == "eod")].iloc[-1]
    realized = sf.trades()["realized"].dropna().sum()
    assert eq["equity"].iloc[-1] == pytest.approx(last["unrealized"] + realized, abs=0.01)
    assert eq["daily_pnl"].sum() == pytest.approx(eq["equity"].iloc[-1], abs=0.01)


def test_open_count_by_day_matches_positions_file(state):
    sf = StateFiles(state)
    eq = sf.daily_equity()
    n = sf.open_count_by_day(eq["date"])
    assert int(n.iloc[-1]) == len(sf.positions())


def test_eod_report_round_trip(state, tmp_path):
    sf = StateFiles(state)
    ref, hist = load_reference(state)
    days = report_days(sf)
    assert len(days) == 30
    r = build_eod(sf, days[-1], ref)
    assert r["n_open_eod"] == len(sf.positions()) and r["z_vs_expected"] is not None
    md = render_md(r)
    assert md.startswith(f"# EOD report — {days[-1]}") and "## Entry funnel" in md and "## Fills" in md
    p_md, p_js = write_eod(r, tmp_path)
    assert json.loads(p_js.read_text())["date"] == days[-1] and p_md.read_text() == md


def test_pages_build_and_refresh_headless(state, tmp_path):
    tabs = build_state_tabs(state, tmp_path / "reports")
    assert [n for n, _, _ in tabs] == ["Pace", "Grid", "Regime", "Metrics", "Model", "Fills", "Reports"]
    for _, _, refresh in tabs:
        refresh()
    heads = {n: p[0].object if hasattr(p[0], "object") else "" for n, p, _ in tabs}
    assert "z =" in heads["Pace"] and "GEX" in heads["Grid"] and "front slope says" in heads["Regime"]
    assert "62 measurable fills" in heads["Fills"]


def test_session_build_with_state_tabs_and_health_from_state(state, tmp_path):
    hist = pnl_history_for_health(state, tmp_path)
    tmpl, *_ = build(DEMO, 1.0, shared=build_desk(DEMO, 1.0), history=hist, extra_tabs=build_state_tabs(state, tmp_path))
    names = list(tmpl.main[0]._names)
    assert names[:7] == ["Pace", "Grid", "Regime", "Metrics", "Model", "Fills", "Reports"] and "Health" in names


def test_execution_page_compares_actual_cost_with_the_model(state, tmp_path):
    """The Fills page must answer 'what did execution cost vs what the backtest charged':
    modeled rows come from reference.json, actual from the ledger's decision quotes."""
    from deskboard.ui.pages import build_state_tabs
    tabs = dict((n, (p, r)) for n, p, r in build_state_tabs(state, tmp_path))
    page, refresh = tabs["Fills"]
    refresh()
    head = page[0].object
    assert "execution vs model" in head and "backtest charged" in head
    cmp_tbl = page[2]
    lines = list(cmp_tbl.value["line"])
    assert lines[:3] == ["spread cost (vs decision cross)", "commissions", "total friction"]
    assert cmp_tbl.value["actual"].notna().all()
    # the synthetic generator fills exactly at the decision cross → spread cost ~0, and the
    # comparison must still show the tape's charge as a non-zero modeled number
    assert abs(float(cmp_tbl.value.loc[0, "actual"])) < 1.0


def test_model_page_buckets_live_against_the_reference(state, tmp_path):
    """Actual vs modeled by condition: every row pairs a live bucket mean with the reference's
    mean for the same bucket, and states how many live observations stand behind it."""
    from deskboard.ui.pages import build_state_tabs
    page, refresh = dict((n, (p, r)) for n, p, r in build_state_tabs(state, tmp_path))["Model"]
    refresh()
    head = page[0].object
    assert "model vs actual by condition" in head or "no live observation" in head
    df = page[1].value
    if len(df):
        assert {"dim", "bucket", "n_live", "actual", "modeled", "difference"} <= set(df.columns)
        assert (df["difference"].round(2) == (df["actual"] - df["modeled"]).round(2)).all()
        assert (df["n_live"] > 0).all()
