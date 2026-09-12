"""Panel app: Risk / P&L / Alerts / Legs / Feed pages driven by `Book.snapshot()` on a timer.

One desk per process: the bus, the replay feed, the limit engine and the Telegram bot are
created once in `serve()` and started on the server's asyncio loop before any browser
connects. A browser tab is just another reader of `Book.snapshot()` — open none, one or
ten and the feed runs once and every alert is pushed once. Nothing in this module
influences the numbers.

    deskboard serve --session data/demo/session_2026-06-15.parquet --speed 20
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pandas as pd
import panel as pn
from bokeh.models import ColumnDataSource
from bokeh.plotting import figure

from ..engine.book import COMPONENTS
from ..engine.desk import Desk
from ..engine.scenarios import ladder
from ..feeds.replay import ReplayFeed, load_session
from ..feeds.synth import demo_blotter

pn.extension("tabulator")

POS_COLS = ["pos_id", "sleeve", "legs", "delta", "delta_usd", "gamma_usd_1pct", "vega_usd_1vol", "theta_usd_day", "pnl",
            "pnl_delta", "pnl_gamma", "pnl_vega", "pnl_theta", "pnl_execution", "pnl_residual"]
LEG_COLS = ["pos_id", "contract", "qty", "mid", "iv", "delta", "delta_usd", "gamma_usd_1pct", "vega_usd_1vol",
            "theta_usd_day", "pnl", "pnl_residual"]
FMT = {c: {"type": "money", "precision": 0} for c in POS_COLS if c.endswith(("usd", "1pct", "1vol", "day")) or c.startswith("pnl")}
FMT.update({"delta": {"type": "money", "precision": 1, "symbol": ""}, "mid": {"type": "money", "precision": 2, "symbol": ""},
            "iv": {"type": "money", "precision": 4, "symbol": ""}, "qty": {"type": "money", "precision": 0, "symbol": ""}})


ALERT_COLS = ["time", "state", "rule", "target", "reason"]


def build_desk(session_path: str, speed: float, blotter: list[dict] | None = None, telegram: bool = False):
    """The process-level state: desk (bus + book + limits), replay feed, optional bot."""
    desk = Desk.build(blotter or demo_blotter())
    feed = ReplayFeed(desk.bus, load_session(session_path), speed=speed)
    bot = None
    if telegram:
        from ..alerts.telegram import from_env
        bot = from_env(desk)
        if bot is None:
            raise SystemExit("set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or drop --telegram)")
        bot.attach(desk.bus)
    return desk, feed, bot


def build(session_path: str, speed: float, blotter: list[dict] | None = None, period_ms: int = 500,
          telegram: bool = False, shared=None):
    """One browser session's widgets. `shared` = (desk, feed, bot) from build_desk; when None
    (tests, notebooks) a private desk is built and its feed started on this session's load."""
    desk, feed, bot = shared if shared is not None else build_desk(session_path, speed, blotter, telegram)
    bus, book = desk.bus, desk.book

    def num(name, fmt="{value:,.0f}", **kw):
        return pn.indicators.Number(name=name, value=0.0, format=fmt, font_size="26pt", title_size="11pt", **kw)

    pnl_ind = num("P&L today ($)", colors=[(0, "#b23b3b"), (1e-9, "#2a7a5a")])
    delta_ind = num("net Δ ($)")
    gamma_ind = num("Γ ($ per 1%)")
    vega_ind = num("ν ($ per vol pt)")
    theta_ind = num("Θ ($ per day)")
    resid_ind = num("residual ($)")
    gap_ind = num("identity gap ($)", "{value:,.4f}")
    spot_ind = num("spot", "{value:,.2f}")
    clock = pn.pane.Markdown("")

    pos_table = pn.widgets.Tabulator(pd.DataFrame(columns=POS_COLS), formatters=FMT, show_index=False,
                                     layout="fit_data_table", height=260, disabled=True)
    leg_table = pn.widgets.Tabulator(pd.DataFrame(columns=LEG_COLS), formatters=FMT, show_index=False,
                                     layout="fit_data_table", height=420, disabled=True)

    src = ColumnDataSource({"component": list(COMPONENTS), "usd": [0.0] * len(COMPONENTS), "color": ["#2458a6"] * len(COMPONENTS)})
    fig = figure(x_range=list(COMPONENTS), height=280, sizing_mode="stretch_width", title="P&L attribution today ($)",
                 toolbar_location=None)
    fig.vbar(x="component", top="usd", width=0.6, source=src, color="color")
    fig.xgrid.grid_line_color = None
    fig.yaxis.axis_label = "$"

    feed_md = pn.pane.Markdown("")
    limits_md = pn.pane.Markdown("")
    ladder_md = pn.pane.Markdown("")
    ladder_table = pn.widgets.Tabulator(pd.DataFrame(), show_index=False, layout="fit_data_table", height=300, disabled=True)
    alert_table = pn.widgets.Tabulator(pd.DataFrame(columns=ALERT_COLS), show_index=False, layout="fit_data_table",
                                       height=360, disabled=True)

    def refresh():
        snap = book.snapshot()
        t = snap["totals"]
        pnl_ind.value = t["pnl"]
        delta_ind.value = t["delta_usd"]
        gamma_ind.value = t["gamma_usd_1pct"]
        vega_ind.value = t["vega_usd_1vol"]
        theta_ind.value = t["theta_usd_day"]
        resid_ind.value = t["pnl_residual"]
        gap_ind.value = t["identity_gap"]
        spot_ind.value = next(iter(t["spot"].values()), float("nan"))
        if t["last_ts"]:
            clock.object = f"**event time** {datetime.fromtimestamp(t['last_ts'], tz=timezone.utc):%Y-%m-%d %H:%M:%S} UTC · {t['n_events']:,} events"
        pos_table.value = pd.DataFrame(snap["positions"])[POS_COLS] if snap["positions"] else pd.DataFrame(columns=POS_COLS)
        leg_table.value = pd.DataFrame(snap["legs"])[LEG_COLS] if snap["legs"] else pd.DataFrame(columns=LEG_COLS)
        vals = [t[f"pnl_{c}"] for c in COMPONENTS]
        src.data = {"component": list(COMPONENTS), "usd": vals, "color": ["#2a7a5a" if v >= 0 else "#b23b3b" for v in vals]}
        open_ = desk.limits.open_breaches()
        limits_md.object = ("**limits** " + (" · ".join(f"🔴 {r} ({tg})" for r, tg in open_) if open_ else "🟢 all inside") +
                            f" · {len(desk.alerts)} alert events" + (f" · telegram: {bot.sent} sent, {bot.failed} failed" if bot else ""))
        if desk.alerts:
            alert_table.value = pd.DataFrame([{"time": datetime.fromtimestamp(a["ts"], tz=timezone.utc).strftime("%H:%M:%S"),
                                               "state": a["state"], "rule": a["rule"], "target": a["target"],
                                               "reason": a["reason"]} for a in reversed(desk.alerts)])
        lad = ladder(book)
        worst, ds, dv = lad.worst()
        rows = pd.DataFrame(lad.to_rows())
        rows.insert(0, "vol shock", [f"{v:+.0%}" if v else "0" for v in lad.vol_shocks])
        ladder_table.value = rows.drop(columns="vol_shock").astype({c: int for c in rows.columns if c not in ("vol shock", "vol_shock")})
        ladder_md.object = (f"**scenario ladder** — full revaluation at the current marks, P&L in $ vs the model price; "
                            f"worst cell **{worst:,.0f}** at spot {ds:+.0%}, vol {dv:+.0%} vol pts"
                            + (f" · not priced: {', '.join(lad.missing)}" if lad.missing else ""))
        lat = bus.latency_ms()
        feed_md.object = (f"**replay** {os.path.basename(session_path)} at {speed}× · {feed.position:,}/{len(feed.df):,} events"
                          f"{' · done' if feed.done.is_set() else ''}\n\n"
                          f"**bus dispatch latency** p50 {lat['p50']:.2f} ms · p99 {lat['p99']:.2f} ms · max {lat.get('max', float('nan')):.2f} ms"
                          + (f"\n\n**telegram** chat {bot.chat_id} · {bot.sent} sent · {bot.failed} failed" if bot else ""))

    def start():
        if shared is None:  # private desk: this session owns the feed
            loop = asyncio.get_event_loop()
            loop.create_task(feed.run())
            if bot is not None:
                loop.create_task(bot.poll())
        refresh()
        pn.state.add_periodic_callback(refresh, period=period_ms)

    pn.state.onload(start)

    risk = pn.Column(pn.Row(pnl_ind, delta_ind, gamma_ind, vega_ind, theta_ind, resid_ind, gap_ind, spot_ind), clock,
                     limits_md, pn.pane.Markdown("### Positions"), pos_table, sizing_mode="stretch_width")
    scen = pn.Column(ladder_md, ladder_table, pn.pane.Markdown(
        "Rows: vol shock in vol points (added to every leg's implied vol). Columns: spot shock. Each cell reprices every leg "
        "with Black-Scholes at the shocked spot and vol, instantaneous (no time roll). The zero cell is 0 by construction; "
        "the ±1 % cells reproduce net Δ$ and Γ$ to first order (tested)."), sizing_mode="stretch_width")
    pnl = pn.Column(pn.pane.Bokeh(fig), pn.pane.Markdown(
        "Attribution between consecutive marks with Greeks at the old mark; **residual = P&L − Σ Greeks − execution**, "
        "reported not hidden. Identity gap is the ledger check and must read $0.0000."), sizing_mode="stretch_width")
    legs = pn.Column(leg_table, sizing_mode="stretch_width")
    feedp = pn.Column(feed_md, sizing_mode="stretch_width")
    rules_md = pn.pane.Markdown("**rules** " + " · ".join(
        f"`{r.name}`: {r.scope} {r.metric} {'>' if r.op == 'max' else '<'} {r.bound:,.0f}" for r in desk.limits.rules))
    alertsp = pn.Column(rules_md, alert_table, sizing_mode="stretch_width")

    tmpl = pn.template.FastListTemplate(title="deskboard", sidebar=[], theme_toggle=False, accent="#2458a6",
                                        main=[pn.Tabs(("Risk", risk), ("P&L", pnl), ("Scenarios", scen), ("Alerts", alertsp),
                                                      ("Legs", legs), ("Feed", feedp))])
    return tmpl, desk, feed


def serve(session_path: str, speed: float, port: int = 5006, show: bool = False, blotter: list[dict] | None = None,
          telegram: bool = False):
    shared = build_desk(session_path, speed, blotter, telegram)
    desk, feed, bot = shared

    def make():
        tmpl, *_ = build(session_path, speed, blotter=blotter, telegram=telegram, shared=shared)
        return tmpl

    def start_tasks():
        loop = asyncio.get_event_loop()
        loop.create_task(feed.run())
        if bot is not None:
            loop.create_task(bot.poll())
            print(f"telegram: pushing alerts to chat {bot.chat_id}; commands /risk /pnl /positions /alerts")

    server = pn.serve(make, port=port, show=show, title="deskboard", autoreload=False, start=False)
    server.io_loop.add_callback(start_tasks)
    server.io_loop.start()
