"""The event tape: ring buffer, per-contract quote ages, per-topic rates; identical on replay and live."""
import asyncio

from deskboard.cli import DEMO
from deskboard.engine.desk import Desk
from deskboard.feeds.replay import ReplayFeed, load_session
from deskboard.feeds.synth import demo_blotter
from deskboard.ui.app import build, build_desk


def test_tape_records_every_topic_and_ages_quotes():
    desk = Desk.build(demo_blotter())
    asyncio.run(ReplayFeed(desk.bus, load_session(DEMO), speed=0.0).run())
    tp = desk.tape
    assert sum(tp.counts.values()) == desk.book.n_events + tp.counts["alert"] + tp.counts.get("clock", 0)
    assert tp.counts["quote"] > 3000 and tp.counts["position"] == 1 and tp.counts["alert"] >= 3
    assert len(tp.events) == tp.maxlen                                  # ring buffer full, oldest dropped
    ages = tp.quote_ages()
    assert ages[0]["sec_type"] == "STK" and ages[0]["age_s"] < 1.0      # spot ticked at the last minute (legs follow it by 0.2 s)
    outage = next(a for a in ages if a["contract"].endswith(":530:C"))
    assert outage["age_s"] < 1.0                                        # outage was mid-session; recovered
    r = tp.rate(5)
    assert r["quote"] > 5 and "position" not in r                       # the 11:00 position event is not in the last 5 min
    recent = tp.recent(5, "position")                                   # … and was evicted from the 2000-event ring by the
    assert len(recent) == 1 and recent[0]["key"] == "A-0615"            # quote flood, but survives in the rare buffer


def test_live_page_builds_and_lists_the_tape():
    shared = build_desk(DEMO, 0.0)                                      # speed 0 = as fast as possible
    desk, feed, _ = shared
    asyncio.run(feed.run())
    tmpl, *_ = build(DEMO, 0.0, shared=shared)
    names = list(tmpl.main[0]._names)
    live = tmpl.main[0].objects[names.index("Live")]
    assert "events since start" in live[0].object and "contracts quoted" in live[0].object
    tape_tbl = live[4]
    assert len(tape_tbl.value) == 200 and set(tape_tbl.value["topic"]) <= {"quote", "fill", "position", "alert", "clock"}
