"""Scenario ladder: zero cell is exactly zero; small shocks reproduce the book's Greeks; missing marks are named."""

import numpy as np
import pytest

from deskboard.engine.desk import Desk
from deskboard.engine.scenarios import ladder
from deskboard.feeds.replay import ReplayFeed, load_session, save_session
from deskboard.feeds.synth import demo_blotter, record


@pytest.fixture(scope="module")
def desk(tmp_path_factory):
    path = tmp_path_factory.mktemp("s") / "session.parquet"
    save_session(record(seed=7), path)
    d = Desk.build(demo_blotter())
    import asyncio
    asyncio.run(ReplayFeed(d.bus, load_session(path)).run())
    return d


def test_zero_shock_cell_is_exactly_zero_and_nothing_missing(desk):
    lad = ladder(desk.book)
    i, j = lad.vol_shocks.index(0.0), lad.spot_shocks.index(0.0)
    assert lad.pnl[i, j] == 0.0
    assert lad.missing == ()


def test_small_spot_shock_matches_delta_dollars_and_gamma(desk):
    t = desk.book.totals()
    lad = ladder(desk.book, spot_shocks=(-0.01, 0.0, 0.01), vol_shocks=(0.0,))
    up, down = lad.pnl[0, 2], lad.pnl[0, 0]
    # first order: (up - down)/2 ≈ delta_usd · 1%; second order: (up + down)/2 ≈ gamma_usd_1pct
    assert (up - down) / 2 == pytest.approx(t["delta_usd"] * 0.01, rel=0.02)
    assert (up + down) / 2 == pytest.approx(t["gamma_usd_1pct"], rel=0.10)


def test_small_vol_shock_matches_vega_dollars(desk):
    t = desk.book.totals()
    lad = ladder(desk.book, spot_shocks=(0.0,), vol_shocks=(-0.01, 0.0, 0.01))
    assert (lad.pnl[2, 0] - lad.pnl[0, 0]) / 2 == pytest.approx(t["vega_usd_1vol"], rel=0.02)


def test_short_roll_rate_matches_theta_dollars_and_a_full_day_shows_convexity(desk):
    t = desk.book.totals()
    small = ladder(desk.book, spot_shocks=(0.0,), vol_shocks=(0.0,), days=0.02).pnl[0, 0] / 0.02
    assert small == pytest.approx(t["theta_usd_day"], rel=0.02)          # the derivative
    one_day = ladder(desk.book, spot_shocks=(0.0,), vol_shocks=(0.0,), days=1.0).pnl[0, 0]
    assert 0.5 * t["theta_usd_day"] < one_day < 1.2 * t["theta_usd_day"]   # a finite roll on 10-day legs is not the derivative


def test_worst_cell_and_rows(desk):
    lad = ladder(desk.book)
    w, ds, dv = lad.worst()
    assert w == lad.pnl.min() and ds in lad.spot_shocks and dv in lad.vol_shocks
    rows = lad.to_rows()
    assert len(rows) == len(lad.vol_shocks) and "+10%" in rows[0] and "-10%" in rows[0] and "0" in rows[0]


def test_leg_without_valid_mark_is_named_not_zeroed():
    from deskboard.bus import Bus
    from deskboard.engine.book import Book
    bus = Bus()
    book = Book()
    book.attach(bus)
    leg = {"symbol": "SYN", "sec_type": "OPT", "expiration": "20260730", "strike": 520.0, "right": "P",
           "side": "BUY", "quantity": 1.0}
    book.load_positions([{"pos_id": "Y", "legs": [leg]}])
    import asyncio
    asyncio.run(bus.publish("quote", 1_781_000_000.0, {"symbol": "SYN", "sec_type": "STK", "bid": 499.9, "ask": 500.1}))
    asyncio.run(bus.publish("quote", 1_781_000_001.0, {**leg, "bid": 9.9, "ask": 10.1}))   # below intrinsic: no IV
    lad = ladder(book)
    assert lad.missing == ("Y:SYN:20260730:520:P",) and np.all(lad.pnl == 0.0)
