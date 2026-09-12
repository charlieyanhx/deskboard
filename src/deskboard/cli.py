"""`deskboard record | replay | serve`."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
DEMO = str(_REPO / "data/demo/session_2026-06-15.parquet") if (_REPO / "data/demo").exists() else "data/demo/session_2026-06-15.parquet"


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
        print(f"events {t['n_events']}  positions {t['n_positions']}  spot {' '.join(f'{k} {v:.2f}' for k, v in t['spot'].items())}")
        print(f"pnl {t['pnl']:.2f} = delta {t['pnl_delta']:.2f} + gamma {t['pnl_gamma']:.2f} + vega {t['pnl_vega']:.2f}"
              f" + theta {t['pnl_theta']:.2f} + execution {t['pnl_execution']:.2f} + residual {t['pnl_residual']:.2f}"
              f"   identity_gap {t['identity_gap']:.4f}")
        print(f"greeks  delta$ {t['delta_usd']:.0f}  gamma$/1% {t['gamma_usd_1pct']:.0f}  vega$/vol {t['vega_usd_1vol']:.0f}"
              f"  theta$/day {t['theta_usd_day']:.0f}")
        for al in desk.alerts:
            print(f"alert {al['state']:<7} {al['rule']:<16} {al['target']:<8} {al['reason']}")
        from .engine.scenarios import ladder
        lad = ladder(desk.book)
        worst, ds, dv = lad.worst()
        print(f"ladder  worst {worst:,.0f} at spot {ds:+.0%} vol {dv:+.0%}"
              f"  |  spot -5%: {lad.pnl[lad.vol_shocks.index(0.0), lad.spot_shocks.index(-0.05)]:,.0f}"
              f"  spot +5%: {lad.pnl[lad.vol_shocks.index(0.0), lad.spot_shocks.index(0.05)]:,.0f}"
              f"  vol +5: {lad.pnl[lad.vol_shocks.index(0.05), lad.spot_shocks.index(0.0)]:,.0f}")
        lat = desk.bus.latency_ms()
        print(f"bus latency ms  p50 {lat['p50']:.2f}  p99 {lat['p99']:.2f}  max {lat['max']:.2f}  n {lat['n']}")
        print(f"state hash {desk.book.state_hash()}")
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
            me = await bot.api.get_me()
            print(f"bot @{me.get('username', '?')} ok; sending to chat {bot.chat_id}")
            await bot.send("deskboard online ✅\n" + format_risk(desk.book.snapshot()))
            print("sent — check Telegram for 'deskboard online' and the risk card")

        try:
            asyncio.run(run())
        except Exception as exc:  # noqa: BLE001 — one line for an operator, no traceback with the token in it
            raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
