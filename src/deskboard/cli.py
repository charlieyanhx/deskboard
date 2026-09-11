"""`deskboard record | replay | serve`."""

from __future__ import annotations

import argparse
import asyncio
import json

DEMO = "data/demo/session_2026-06-15.parquet"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="deskboard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record the synthetic demo session")
    r.add_argument("--out", default=DEMO)
    r.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("replay", help="replay a session headlessly; print totals, latency, state hash")
    p.add_argument("--session", default=DEMO)
    p.add_argument("--speed", type=float, default=0.0)
    p.add_argument("--blotter", default=None, help="positions file (demo blotter or live bot state); default: built-in demo")
    s = sub.add_parser("serve", help="serve the dashboard")
    s.add_argument("--blotter", default=None)
    s.add_argument("--session", default=DEMO)
    s.add_argument("--speed", type=float, default=20.0)
    s.add_argument("--port", type=int, default=5006)
    s.add_argument("--show", action="store_true")
    s.add_argument("--telegram", action="store_true", help="push alerts to Telegram and answer commands (env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    t = sub.add_parser("telegram-test", help="send one test message to the configured chat")
    t.add_argument("--session", default=DEMO)
    a = ap.parse_args(argv)

    if a.cmd == "record":
        from .feeds.replay import save_session
        from .feeds.synth import demo_blotter, record
        df = record(seed=a.seed)
        save_session(df, a.out)
        blotter = str(a.out).rsplit("/", 1)[0] + "/blotter.json"
        __import__("pathlib").Path(blotter).write_text(json.dumps(demo_blotter(), indent=1))
        print(f"wrote {len(df)} events to {a.out} and the demo blotter to {blotter}")
    elif a.cmd == "replay":
        from .engine.desk import Desk
        from .feeds.blotter import load_blotter
        from .feeds.replay import ReplayFeed, load_session
        from .feeds.synth import demo_blotter

        async def run():
            desk = Desk.build(load_blotter(a.blotter) if a.blotter else demo_blotter())
            await ReplayFeed(desk.bus, load_session(a.session), speed=a.speed).run()
            return desk

        desk = asyncio.run(run())
        t = desk.book.totals()
        print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in t.items()}, indent=1))
        for al in desk.alerts:
            print(f"alert {al['state']:<7} {al['rule']:<16} {al['target']:<8} {al['reason']}")
        print("bus latency ms:", {k: round(v, 3) for k, v in desk.bus.latency_ms().items()})
        print("state hash:", desk.book.state_hash())
    elif a.cmd == "serve":
        from .feeds.blotter import load_blotter
        from .ui.app import serve
        serve(a.session, a.speed, port=a.port, show=a.show, blotter=load_blotter(a.blotter) if a.blotter else None,
              telegram=a.telegram)
    elif a.cmd == "telegram-test":
        from .alerts.telegram import format_risk, from_env
        from .engine.desk import Desk
        from .feeds.synth import demo_blotter

        async def run():
            desk = Desk.build(demo_blotter())
            bot = from_env(desk)
            if bot is None:
                raise SystemExit("set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
            await bot.send("deskboard online ✅\n" + format_risk(desk.book.snapshot()))
            print("sent")

        asyncio.run(run())


if __name__ == "__main__":
    main()
