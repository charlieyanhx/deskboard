import asyncio

import pytest

from deskboard.alerts.telegram import TelegramBot, answer, format_alert, format_pnl, format_risk
from deskboard.bus import Bus
from deskboard.engine.desk import Desk
from deskboard.engine.limits import LimitEngine, Rule
from deskboard.feeds.replay import ReplayFeed, save_session
from deskboard.feeds.synth import demo_blotter, record


def _snap(delta_usd, pnl, positions=()):
    return {"totals": {"delta_usd": delta_usd, "pnl": pnl, "gamma_usd_1pct": 0, "vega_usd_1vol": 0, "theta_usd_day": 0,
                       "identity_gap": 0.0, "n_positions": len(positions), "spot": {"SYN": 500.0}, "last_ts": 1.0,
                       **{f"pnl_{c}": 0.0 for c in ("delta", "gamma", "vega", "theta", "execution", "residual")}},
            "positions": list(positions), "legs": []}


async def test_alert_only_on_state_change_with_hysteresis():
    eng = LimitEngine([Rule("d", "book", "delta_usd", "max", 1000)], clear_margin=0.05)
    assert await eng.check(_snap(900, 0), 1) == []
    a = await eng.check(_snap(1200, 0), 2)
    assert len(a) == 1 and a[0].state == "BREACH" and "1,200 > limit 1,000" in a[0].reason
    assert await eng.check(_snap(1300, 0), 3) == []           # still breached: no repeat
    assert await eng.check(_snap(980, 0), 4) == []            # inside but within the 5 % band: no flap
    b = await eng.check(_snap(940, 0), 5)
    assert len(b) == 1 and b[0].state == "CLEARED"
    assert eng.open_breaches() == []


async def test_position_scope_and_bus_publication():
    bus = Bus()
    got = []
    bus.subscribe("alert", lambda ev: got.append(ev.payload))
    eng = LimitEngine([Rule("loss", "position", "pnl", "min", -100)], bus=bus)
    pos = [{"pos_id": "A", "pnl": -150.0, "delta_usd": 0}, {"pos_id": "B", "pnl": 20.0, "delta_usd": 0}]
    await eng.check(_snap(0, 0, pos), 9)
    assert [g["target"] for g in got] == ["A"] and got[0]["state"] == "BREACH"
    assert eng.open_breaches() == [("loss", "A")]


async def test_nan_never_breaches():
    eng = LimitEngine([Rule("d", "book", "delta_usd", "max", 1)])
    assert await eng.check(_snap(float("nan"), 0), 1) == []


class FakeAPI:
    def __init__(self):
        self.sent, self.updates = [], []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def get_updates(self, offset, timeout):
        u, self.updates = self.updates, []
        return u


async def test_bot_pushes_alerts_and_answers_only_its_chat(tmp_path):
    path = tmp_path / "s.parquet"
    save_session(record(seed=7), path)
    desk = Desk.build(demo_blotter())
    api = FakeAPI()
    bot = TelegramBot(api, "42", desk)
    bot.attach(desk.bus)
    from deskboard.feeds.replay import load_session
    await ReplayFeed(desk.bus, load_session(path)).run()
    assert bot.sent == len(desk.alerts) > 0
    assert all("BREACH" in t or "CLEARED" in t for _, t in api.sent)
    # commands
    assert await bot.handle_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": "/risk"}})
    assert "Risk" in api.sent[-1][1] and "identity gap 0.0000" in api.sent[-1][1]
    n = len(api.sent)
    assert await bot.handle_update({"update_id": 2, "message": {"chat": {"id": 999}, "text": "/risk"}}) is None
    assert len(api.sent) == n                                   # foreign chat ignored
    assert await bot.handle_update({"update_id": 3, "message": {"chat": {"id": 42}, "text": "hello"}}) is None
    assert "attribution" in (await bot.handle_update({"update_id": 4, "message": {"chat": {"id": 42}, "text": "/pnl@deskbot"}}))


async def test_poll_loop_drains_and_stops():
    desk = Desk.build(demo_blotter())
    api = FakeAPI()
    api.updates = [{"update_id": 7, "message": {"chat": {"id": 1}, "text": "/help"}}]
    bot = TelegramBot(api, "1", desk)
    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(bot.poll(stop, timeout=0, skip_backlog=False), stopper())
    assert any("Commands" in t for _, t in api.sent)


def test_formatters_escape_html_and_cover_components():
    desk = Desk.build(demo_blotter())
    snap = desk.book.snapshot()
    assert "P&amp;L" in format_risk(snap) and "<pre>" in format_pnl(snap)
    a = {"ts": 1.0, "rule": "r<1>", "scope": "book", "target": "book", "metric": "pnl", "value": -1, "bound": 0,
         "state": "BREACH", "reason": "x < y"}
    assert "r&lt;1&gt;" in format_alert(a) and "x &lt; y" in format_alert(a)
    assert answer("/nope", desk) is None and answer("", desk) is None


@pytest.mark.parametrize("cmd", ["/risk", "/pnl", "/positions", "/alerts", "/help"])
def test_every_command_answers(cmd):
    assert answer(cmd, Desk.build(demo_blotter()))


class FailingAPI(FakeAPI):
    async def send_message(self, chat_id, text):
        from deskboard.alerts.telegram import TelegramError
        raise TelegramError("telegram sendMessage failed: 403 Forbidden: bot can't initiate conversation with a user")


async def test_telegram_failure_never_stops_the_book(tmp_path):
    path = tmp_path / "s.parquet"
    save_session(record(seed=7), path)
    desk = Desk.build(demo_blotter())
    bot = TelegramBot(FailingAPI(), "42", desk)
    bot.attach(desk.bus)
    from deskboard.feeds.replay import load_session
    await ReplayFeed(desk.bus, load_session(path)).run()          # would raise inside the bus chain if unhandled
    assert bot.failed == len(desk.alerts) > 0 and bot.sent == 0
    assert desk.book.totals()["n_events"] == 3503


def test_error_explanations_include_description_hint_and_no_token():
    import httpx

    from deskboard.alerts.telegram import _explain
    r = httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"},
                       request=httpx.Request("POST", "https://api.telegram.org/bot123:SECRET/sendMessage"))
    msg = str(_explain(r, "sendMessage", "123:SECRET"))
    assert "chat not found" in msg and "TELEGRAM_CHAT_ID" in msg and "SECRET" not in msg
    r403 = httpx.Response(403, json={"description": "Forbidden: bot can't initiate conversation with a user"},
                          request=httpx.Request("POST", "https://x"))
    assert "press Start" in str(_explain(r403, "sendMessage", "t"))


async def test_start_command_is_answered_and_backlog_skipped():
    desk = Desk.build(demo_blotter())
    api = FakeAPI()
    api.updates = [{"update_id": 5, "message": {"chat": {"id": 1}, "text": "/risk"}}]   # stale, from before start
    bot = TelegramBot(api, "1", desk)
    stop = asyncio.Event()

    async def later():
        await asyncio.sleep(0.03)
        api.updates = [{"update_id": 6, "message": {"chat": {"id": 1}, "text": "/start"}}]
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(bot.poll(stop, timeout=0), later())
    texts = [t for _, t in api.sent]
    assert any("Commands" in t for t in texts) and not any("<b>Risk</b>" in t for t in texts)
