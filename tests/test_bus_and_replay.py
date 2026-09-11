"""Bus ordering, replay determinism (same file → same hash, at any speed), latency budget."""

import asyncio

import pytest

from deskboard.bus import Bus
from deskboard.engine.book import COMPONENTS, Book
from deskboard.feeds.replay import ReplayFeed, load_session, save_session
from deskboard.feeds.synth import demo_blotter, record

LATENCY_BUDGET_P99_MS = 10.0


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    path = tmp_path_factory.mktemp("s") / "session.parquet"
    save_session(record(seed=7), path)
    return path


async def _replay(path, speed=0.0):
    bus = Bus()
    book = Book()
    book.attach(bus)
    book.load_positions(demo_blotter())
    await ReplayFeed(bus, load_session(path), speed=speed).run()
    return book, bus


async def test_bus_preserves_publish_order_and_awaits_async_handlers():
    bus = Bus()
    seen = []

    def sync_h(ev):
        seen.append(("s", ev.seq))

    async def async_h(ev):
        await asyncio.sleep(0)
        seen.append(("a", ev.seq))

    bus.subscribe("quote", sync_h)
    bus.subscribe("quote", async_h)
    for i in range(5):
        await bus.publish("quote", float(i), {"i": i})
    assert seen == [(k, s) for s in range(1, 6) for k in ("s", "a")]
    with pytest.raises(ValueError):
        bus.subscribe("nope", sync_h)


async def test_recording_is_deterministic(tmp_path):
    a, b = record(seed=7), record(seed=7)
    assert a.equals(b)
    assert not a.equals(record(seed=8))


async def test_replay_same_file_same_state_hash(session):
    b1, _ = await _replay(session)
    b2, _ = await _replay(session)
    assert b1.state_hash() == b2.state_hash()


async def test_speed_does_not_change_state(session):
    """Wall-clock pacing only delays delivery; it never changes the numbers."""
    b0, _ = await _replay(session, speed=0.0)
    b1, _ = await _replay(session, speed=1e6)
    assert b0.state_hash() == b1.state_hash()


async def test_attribution_identity_holds_for_every_position(session):
    book, _ = await _replay(session)
    for r in book.position_rows():
        assert r["pnl"] == pytest.approx(sum(r[f"pnl_{c}"] for c in COMPONENTS), abs=1e-9), r["pos_id"]
    t = book.totals()
    assert abs(t["identity_gap"]) < 1e-9
    assert t["n_positions"] == 4 and t["n_events"] == 3503


async def test_residual_is_small_and_execution_is_a_cost(session):
    book, _ = await _replay(session)
    t = book.totals()
    assert abs(t["pnl_residual"]) < 0.05 * max(1.0, abs(t["pnl_delta"]))
    intraday = next(r for r in book.position_rows() if r["pos_id"] == "A-0615")
    assert intraday["pnl_execution"] < 0          # crossed the spread on both legs


async def test_sell_flips_signs():
    from deskboard.engine.book import signed_qty
    assert signed_qty({"quantity": 3, "side": "SELL"}) == -3 and signed_qty({"quantity": 3, "side": "BUY"}) == 3


async def test_latency_budget(session):
    _, bus = await _replay(session)
    lat = bus.latency_ms()
    assert lat["n"] == 3503
    assert lat["p99"] < LATENCY_BUDGET_P99_MS, lat


def test_blotter_loader_accepts_demo_and_live_state_shapes(tmp_path):
    from deskboard.feeds.blotter import load_blotter
    demo = demo_blotter()
    (tmp_path / "demo.json").write_text(__import__("json").dumps(demo))
    (tmp_path / "state.json").write_text(__import__("json").dumps({"positions": demo, "cum_pnl": 0}))
    a, b = load_blotter(tmp_path / "demo.json"), load_blotter(tmp_path / "state.json")
    assert [p["pos_id"] for p in a] == [p["pos_id"] for p in b] == [p["pos_id"] for p in demo]
    assert a[1]["hedge_shares"] == -40.0
    bad = [{"pos_id": "x", "legs": [{"symbol": "SYN", "strike": 1}]}]
    (tmp_path / "bad.json").write_text(__import__("json").dumps(bad))
    with pytest.raises(ValueError):
        load_blotter(tmp_path / "bad.json")
