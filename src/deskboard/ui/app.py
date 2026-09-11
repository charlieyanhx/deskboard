"""Panel app: Risk / P&L / Legs / Feed pages driven by `Book.snapshot()` on a timer.

The bus and the replay feed run as tasks on the server's own asyncio loop; the UI only
reads state. Nothing in this module influences the numbers — swap it for a TUI and the
engine does not notice.

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

from ..bus import Bus
from ..engine.book import COMPONENTS, Book
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


def build(session_path: str, speed: float, blotter: list[dict] | None = None, period_ms: int = 500):
    bus = Bus()
    book = Book()
    book.attach(bus)
    book.load_positions(blotter or demo_blotter())
    feed = ReplayFeed(bus, load_session(session_path), speed=speed)

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
        lat = bus.latency_ms()
        feed_md.object = (f"**replay** {os.path.basename(session_path)} at {speed}× · {feed.position:,}/{len(feed.df):,} events"
                          f"{' · done' if feed.done.is_set() else ''}\n\n"
                          f"**bus dispatch latency** p50 {lat['p50']:.2f} ms · p99 {lat['p99']:.2f} ms · max {lat.get('max', float('nan')):.2f} ms")

    def start():
        asyncio.get_event_loop().create_task(feed.run())
        pn.state.add_periodic_callback(refresh, period=period_ms)

    pn.state.onload(start)

    risk = pn.Column(pn.Row(pnl_ind, delta_ind, gamma_ind, vega_ind, theta_ind, resid_ind, gap_ind, spot_ind), clock,
                     pn.pane.Markdown("### Positions"), pos_table, sizing_mode="stretch_width")
    pnl = pn.Column(pn.pane.Bokeh(fig), pn.pane.Markdown(
        "Attribution between consecutive marks with Greeks at the old mark; **residual = P&L − Σ Greeks − execution**, "
        "reported not hidden. Identity gap is the ledger check and must read $0.0000."), sizing_mode="stretch_width")
    legs = pn.Column(leg_table, sizing_mode="stretch_width")
    feedp = pn.Column(feed_md, sizing_mode="stretch_width")

    tmpl = pn.template.FastListTemplate(title="deskboard", sidebar=[], theme_toggle=False, accent="#2458a6",
                                        main=[pn.Tabs(("Risk", risk), ("P&L", pnl), ("Legs", legs), ("Feed", feedp))])
    return tmpl, book, bus, feed


def serve(session_path: str, speed: float, port: int = 5006, show: bool = False, blotter: list[dict] | None = None):
    def make():
        tmpl, *_ = build(session_path, speed, blotter=blotter)
        return tmpl
    pn.serve(make, port=port, show=show, title="deskboard", autoreload=False)
