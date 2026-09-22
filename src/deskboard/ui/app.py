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
from ..engine.health import assess, full_calendar, load_history, rolling_sharpe
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

# A desk is read on a phone as often as on a monitor. Below 820 px: the tab strip scrolls
# horizontally instead of wrapping into three lines, the indicator row wraps and shrinks,
# and every table scrolls inside its own box so the page itself never scrolls sideways.
MOBILE_CSS = """
.bk-Tabs > .bk-header, .bk-tabs-header { overflow-x: auto; -webkit-overflow-scrolling: touch; flex-wrap: nowrap; }
.bk-Tabs > .bk-header::-webkit-scrollbar { height: 3px; }
.tabulator { max-width: 100%; }
@media (max-width: 820px) {
  .bk-Tabs > .bk-header .bk-tab, .bk-tabs-header .bk-tab { padding: 6px 10px; font-size: 13px; white-space: nowrap; }
  .pn-indicator-number, .bk-clearfix + div .value { font-size: 20px !important; }
  div[class*="indicator"] { min-width: 120px !important; }
  .bk-panel-models-layout-Column, .bk-panel-models-layout-Row { max-width: 100vw; }
  .tabulator, .bk-panel-models-tabulator-DataTabulator { overflow-x: auto !important; font-size: 12px; }
  .bk-panel-models-markup-HTML, .markdown { font-size: 13px; line-height: 1.45; }
  .bk-Figure, .bk-plot-wrapper { max-width: 100% !important; }
  #header .bk-Row { gap: 4px; }
  #header .title { font-size: 18px; }
}
"""


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
          telegram: bool = False, shared=None, history: str | None = None, extra_tabs=None, slow_refresh_s: float = 30.0):
    """One browser session's widgets. `shared` = (desk, feed, bot) from build_desk; when None
    (tests, notebooks) a private desk is built and its feed started on this session's load.

    ``extra_tabs`` is the plugin surface: a list of ``(name, panel, refresh)`` where ``refresh``
    is a no-argument callable that re-reads whatever the page is built on and repaints. Extra
    pages go in front of the built-in ones, are refreshed once at load, every ``slow_refresh_s``
    after that (files change per snap, not per tick), and whenever the header ↻ is pressed."""
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
    live_md = pn.pane.Markdown("")
    topic_sel = pn.widgets.RadioButtonGroup(name="topic", options=["all", "quote", "fill", "position", "alert", "clock"], value="all")
    tape_table = pn.widgets.Tabulator(pd.DataFrame(columns=["time", "topic", "key", "detail", "age s"]), show_index=False,
                                      layout="fit_data_table", height=360, disabled=True, pagination=None)
    ages_table = pn.widgets.Tabulator(pd.DataFrame(columns=["contract", "bid", "ask", "spread", "age_s"]), show_index=False,
                                      layout="fit_data_table", height=300, disabled=True, pagination=None,
                                      formatters={"age_s": {"type": "money", "precision": 1, "symbol": ""}})
    spot_src = ColumnDataSource({"t": [], "mid": []})
    spot_fig = figure(height=200, sizing_mode="stretch_width", x_axis_type="datetime", toolbar_location=None,
                      title="underlying mid — last 600 quotes")
    spot_fig.line("t", "mid", source=spot_src, color="#2458a6")
    rate_src = ColumnDataSource({"topic": [], "per_min": []})
    rate_fig = figure(x_range=[], height=180, sizing_mode="stretch_width", toolbar_location=None,
                      title="events per minute by topic — last 5 event-clock minutes")
    rate_fig.vbar(x="topic", top="per_min", width=0.6, source=rate_src, color="#2a7a5a")
    limits_md = pn.pane.Markdown("")
    ladder_md = pn.pane.Markdown("")
    exec_md = pn.pane.Markdown("")
    exec_table = pn.widgets.Tabulator(pd.DataFrame(), show_index=False, layout="fit_data_table", height=320, disabled=True,
                                      formatters={"fill": {"type": "money", "precision": 3, "symbol": ""},
                                                  "mid": {"type": "money", "precision": 3, "symbol": ""},
                                                  "half_spread": {"type": "money", "precision": 3, "symbol": ""},
                                                  "usd_per_contract": {"type": "money", "precision": 2},
                                                  "frac_half_spread": {"type": "money", "precision": 2, "symbol": ""}})
    ladder_table = pn.widgets.Tabulator(pd.DataFrame(), show_index=False, layout="fit_data_table", height=300, disabled=True)
    alert_table = pn.widgets.Tabulator(pd.DataFrame(columns=ALERT_COLS), show_index=False, layout="fit_data_table",
                                       height=360, disabled=True)
    healthp = _health_page(history, book) if history else pn.Column(pn.pane.Markdown(
        "**health** no P&L history given — `deskboard serve --history path.csv` (columns date, pnl[, backtest])"))

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
        if book.fills:
            fx = pd.DataFrame(book.fills)
            fx["time"] = pd.to_datetime(fx["ts"], unit="s", utc=True).dt.strftime("%H:%M:%S")
            exec_table.value = fx[["time", "pos_id", "contract", "side", "qty", "fill", "mid", "half_spread",
                                   "usd_per_contract", "frac_half_spread"]]
            tot = (fx["usd_per_contract"] * fx["qty"]).sum()
            exec_md.object = (f"**execution** {len(fx)} fills · cost vs mid at fill **${tot:,.2f}** · "
                              f"mean {fx['frac_half_spread'].mean():.2f} of the half-spread paid "
                              f"(1.0 = crossed the spread, 0 = at mid, negative = price improvement)")
        else:
            exec_md.object = "**execution** no fills yet this session"
        if history:
            healthp.refresh()
        _refresh_live()
        lat = bus.latency_ms()
        feed_md.object = (f"**replay** {os.path.basename(session_path)} at {speed}× · {feed.position:,}/{len(feed.df):,} events"
                          f"{' · done' if feed.done.is_set() else ''}\n\n"
                          f"**bus dispatch latency** p50 {lat['p50']:.2f} ms · p99 {lat['p99']:.2f} ms · max {lat.get('max', float('nan')):.2f} ms"
                          + (f"\n\n**telegram** chat {bot.chat_id} · {bot.sent} sent · {bot.failed} failed" if bot else ""))

    extra = list(extra_tabs or [])

    def refresh_extra():
        for name, _panel, fn in extra:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — one page's failure must not stop the others
                import logging
                logging.getLogger("deskboard.ui").warning("page %s refresh failed: %s", name, exc)

    tick = {"n": 0}

    def refresh_extra_slow():
        tick["n"] += 1
        if tick["n"] == 1 or tick["n"] % max(1, int(slow_refresh_s * 1000 / period_ms)) == 0:
            refresh_extra()

    def _refresh_live():
        tp = desk.tape
        now_ts = tp.events[-1]["ts"] if tp.events else None
        rows = tp.recent(200, None if topic_sel.value == "all" else topic_sel.value)
        if rows:
            tape_table.value = pd.DataFrame([{
                "time": datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3], "topic": r["topic"],
                "key": r["key"], "detail": r["detail"], "age s": round(now_ts - r["ts"], 1)} for r in rows])
        ages = tp.quote_ages(now_ts)
        if ages:
            ages_table.value = pd.DataFrame(ages)[["contract", "bid", "ask", "spread", "age_s"]]
        spots = set(book.totals()["spot"])
        spot_rows = [r for r in tp.events if r["topic"] == "quote" and r.get("key") in spots][-600:] if spots else []
        if spot_rows:
            mids = [(datetime.fromtimestamp(r["ts"], tz=timezone.utc), _mid_of(r["detail"])) for r in spot_rows]
            spot_src.data = {"t": [m[0] for m in mids], "mid": [m[1] for m in mids]}
        rate = tp.rate(5, now_ts)
        rate_fig.x_range.factors = list(rate)
        rate_src.data = {"topic": list(rate), "per_min": list(rate.values())}
        stale = [a for a in ages if a["age_s"] > 120]
        live_md.object = (f"**live** {sum(tp.counts.values()):,} events since start · " +
                          " · ".join(f"{k} {v:,}" for k, v in tp.counts.items()) +
                          (f" · last event {datetime.fromtimestamp(now_ts, tz=timezone.utc):%H:%M:%S} UTC" if now_ts else "") +
                          f" · {len(ages)} contracts quoted" + (f" · **{len(stale)} stale > 120 s**" if stale else " · all fresh"))

    topic_sel.param.watch(lambda *_: _refresh_live(), "value")

    def guarded(fn, name):
        """A refresh that raises must say so: Panel swallows exceptions in session callbacks,
        and a silent one leaves every widget at its initial value (2026-09-22: a whole page of
        zeros with a healthy feed behind it)."""
        def run():
            try:
                fn()
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("deskboard.ui").exception("%s refresh failed", name)
        return run

    def start():
        if shared is None:  # private desk: this session owns the feed
            loop = asyncio.get_event_loop()
            loop.create_task(feed.run())
            if bot is not None:
                loop.create_task(bot.poll())
        guarded(refresh, "desk")()
        guarded(refresh_extra_slow, "pages")()
        pn.state.add_periodic_callback(guarded(refresh, "desk"), period=period_ms)
        if extra:
            pn.state.add_periodic_callback(guarded(refresh_extra_slow, "pages"), period=period_ms)

    pn.state.onload(start)

    risk = pn.Column(pn.Row(pnl_ind, delta_ind, gamma_ind, vega_ind, theta_ind, resid_ind, gap_ind, spot_ind), clock,
                     limits_md, pn.pane.Markdown(
        "Book totals and the live limit state. Per-position P&L and its attribution are on the **Positions** page."),
        sizing_mode="stretch_width")
    scen = pn.Column(ladder_md, ladder_table, pn.pane.Markdown(
        "Rows: vol shock in vol points (added to every leg's implied vol). Columns: spot shock. Each cell reprices every leg "
        "with Black-Scholes at the shocked spot and vol, instantaneous (no time roll). The zero cell is 0 by construction; "
        "the ±1 % cells reproduce net Δ$ and Γ$ to first order (tested)."), sizing_mode="stretch_width")
    pnl = pn.Column(pn.pane.Markdown("### Positions — P&L and where it came from"), pos_table,
                    pn.pane.Bokeh(fig), pn.pane.Markdown(
        "One row per position: P&L today and its split into delta / gamma / vega / theta / execution / **residual**. "
        "Attribution is taken between consecutive marks with the Greeks at the old mark; residual = P&L − Σ Greeks − "
        "execution and is reported, never hidden — a residual that grows is a marks or Greeks problem before it is a "
        "strategy one. The bars are the same split for the book. Identity gap must read $0.0000."),
        sizing_mode="stretch_width")
    legs = pn.Column(leg_table, sizing_mode="stretch_width")
    execp = pn.Column(exec_md, exec_table, pn.pane.Markdown(
        "Each fill against the mid at the moment it printed, in the units tcakit reports: $ per contract and fraction of the "
        "half-spread. The sum is the `execution` line of the P&L attribution. Arrival, interval VWAP and reversion benchmarks "
        "need the full order lifecycle and live in [tcakit](https://github.com/charlieyanhx/tcakit)."), sizing_mode="stretch_width")
    feedp = pn.Column(feed_md, sizing_mode="stretch_width")
    livep = pn.Column(live_md, pn.pane.Bokeh(spot_fig), pn.Row(pn.Column(pn.pane.Markdown("### Quote ages"), ages_table),
                      pn.Column(pn.pane.Markdown("### Rate"), pn.pane.Bokeh(rate_fig)), sizing_mode="stretch_width"),
                      pn.Row(pn.pane.Markdown("### Tape (newest first)"), topic_sel), tape_table, pn.pane.Markdown(
        "Every event the bus dispatched, newest first, with its age on the event clock. Quote ages are the seconds since a "
        "contract last ticked — on a live feed a contract that stops ticking ages here before anything else notices. "
        "Counts and rates are per topic; the underlying line is the last 600 spot quotes."), sizing_mode="stretch_width")
    rules_md = pn.pane.Markdown("**rules** " + " · ".join(
        f"`{r.name}`: {r.scope} {r.metric} {'>' if r.op == 'max' else '<'} {r.bound:,.0f}" for r in desk.limits.rules))
    alertsp = pn.Column(rules_md, alert_table, sizing_mode="stretch_width")

    tabs = pn.Tabs(*[(n, p) for n, p, _ in extra], ("Risk", risk), ("Positions", pnl), ("Scenarios", scen), ("Execution", execp),
                   ("Health", healthp), ("Alerts", alertsp), ("Legs", legs), ("Live", livep), ("Feed", feedp))
    names = list(tabs._names)
    want = (pn.state.session_args.get("tab", [b""])[0].decode() if pn.state.session_args else "")
    if want in names:                       # ?tab=Health opens on that page (wall monitors, screenshots)
        tabs.active = names.index(want)
    # Header ↻: repaint every page from its source now, without a browser reload (a reload
    # would drop the websocket session and its replay position).
    refresh_btn = pn.widgets.Button(name="↻ refresh", button_type="light", width=110)
    refresh_note = pn.pane.Markdown("", margin=(8, 4), styles={"color": "white"})

    def manual_refresh(*_):
        refresh()
        refresh_extra()
        refresh_note.object = f"refreshed {datetime.now(tz=timezone.utc):%H:%M:%S} UTC"

    refresh_btn.on_click(manual_refresh)
    tmpl = pn.template.FastListTemplate(title="deskboard", sidebar=[], theme_toggle=False, accent="#2458a6", main=[tabs],
                                        header=[pn.Row(refresh_btn, refresh_note)],
                                        meta_viewport="width=device-width, initial-scale=1, viewport-fit=cover")
    tmpl.config.raw_css = [MOBILE_CSS]
    return tmpl, desk, feed


def _mid_of(detail: str) -> float:
    try:
        b, a = detail.split(" / ")
        return (float(b) + float(a)) / 2.0
    except (ValueError, AttributeError):
        return float("nan")


class _health_page(pn.Column):
    """Health tab: the history file plus today's live P&L as a provisional last day, re-assessed on every refresh."""

    def __init__(self, history: str, book):
        self.live, self.backtest = load_history(history)
        self.book = book
        self.today = pd.Timestamp(datetime.fromtimestamp(book.totals()["last_ts"] or 0, tz=timezone.utc).date()) \
            if book.totals()["last_ts"] else None
        self.md = pn.pane.Markdown("")
        self.table = pn.widgets.Tabulator(pd.DataFrame(columns=["metric", "value"]), show_index=False,
                                          layout="fit_data_table", height=240, disabled=True)
        self.src = ColumnDataSource({"date": [], "cum": [], "dd": [], "rs": [], "cusum": []})
        f1 = figure(height=220, sizing_mode="stretch_width", x_axis_type="datetime", title="cumulative P&L ($) and drawdown",
                    toolbar_location=None)
        f1.line("date", "cum", source=self.src, color="#2458a6", legend_label="cumulative")
        f1.varea("date", "dd", 0, source=self.src, color="#b23b3b", alpha=0.35, legend_label="drawdown")
        f1.legend.location = "top_left"
        f2 = figure(height=180, sizing_mode="stretch_width", x_axis_type="datetime", x_range=f1.x_range,
                    title="rolling 63-day Sharpe (full calendar)", toolbar_location=None)
        f2.line("date", "rs", source=self.src, color="#2a7a5a")
        f3 = figure(height=180, sizing_mode="stretch_width", x_axis_type="datetime", x_range=f1.x_range,
                    title="live vs backtest: one-sided CUSUM (flag above the line)", toolbar_location=None)
        f3.line("date", "cusum", source=self.src, color="#b23b3b")
        self.hline = f3.line([], [], color="#666", line_dash="dashed")
        super().__init__(self.md, pn.pane.Bokeh(f1), pn.pane.Bokeh(f2), pn.pane.Bokeh(f3), self.table,
                         pn.pane.Markdown(
            "Every statistic is on the full business-day calendar (inactive days are $0, never dropped). Sharpe is "
            "√252 · mean / std of daily dollars and is quoted with its window. The CUSUM accumulates the normalised "
            "shortfall of live against the backtest's expected P&L (allowance 0.5 std/day, threshold 8: 2.7 % false "
            "flags per 500 days, median 23-day delay on a 0.8-std fade, by simulation). Today's live P&L is appended "
            "as a provisional last day."), sizing_mode="stretch_width")
        self.refresh()

    def refresh(self):
        live = self.live
        if self.today is not None and self.today > live.index.max():
            live = pd.concat([live, pd.Series([self.book.totals()["pnl"]], index=[self.today])])
        h = assess(live, self.backtest)
        rows = h.rows()
        self.table.value = pd.DataFrame(rows)
        cal = full_calendar(live)
        rs = rolling_sharpe(cal, 63)
        dd = h.drawdown.series
        cus = h.divergence.cusum.reindex(cal.index) if h.divergence else pd.Series(float("nan"), index=cal.index)
        self.src.data = {"date": cal.index, "cum": cal.cumsum().to_numpy(), "dd": dd.to_numpy(), "rs": rs.to_numpy(),
                         "cusum": cus.to_numpy()}
        if h.divergence:
            self.hline.data_source.data = {"x": [cal.index[0], cal.index[-1]], "y": [h.divergence.threshold] * 2}
        flag = h.divergence.reason() if h.divergence else "no backtest column"
        self.md.object = (f"**health** {h.days} days · Sharpe {h.sharpe_full:.2f} full / {h.sharpe_63d:.2f} last 63 / "
                          f"{h.sharpe_252d:.2f} last 252 · max DD {h.drawdown.max_dd:,.0f} · "
                          f"{'🔴' if h.divergence and h.divergence.crossed else '🟢'} {flag}")


def serve(session_path: str, speed: float, port: int = 5006, show: bool = False, blotter: list[dict] | None = None,
          telegram: bool = False, history: str | None = None, extra_tabs_factory=None, exit_on_feed_end: bool = False):
    """``extra_tabs_factory``: no-arg callable returning ``build``'s ``extra_tabs`` for one browser
    session (called per session, so widgets are not shared across tabs). ``exit_on_feed_end``: a
    feed that finishes or dies takes the process down (exit 3) so a container restart policy brings
    it back against a fresh source — for a live connector; a replay simply ends."""
    shared = build_desk(session_path, speed, blotter, telegram)
    desk, feed, bot = shared

    def make():
        tmpl, *_ = build(session_path, speed, blotter=blotter, telegram=telegram, shared=shared, history=history,
                         extra_tabs=extra_tabs_factory() if extra_tabs_factory else None)
        return tmpl

    def start_tasks():
        loop = asyncio.get_event_loop()

        async def run_feed():
            try:
                await feed.run()
            except Exception as exc:  # noqa: BLE001
                if exit_on_feed_end:
                    import logging
                    logging.getLogger("deskboard").critical("feed died: %s — exiting for restart", exc)
                    os._exit(3)
                raise
            if exit_on_feed_end:
                os._exit(3)

        loop.create_task(run_feed())
        if bot is not None:
            loop.create_task(bot.poll())
            print(f"telegram: pushing alerts to chat {bot.chat_id}; commands /risk /pnl /positions /alerts")

    # Bokeh only accepts websocket upgrades whose Origin is allow-listed. DESKBOARD_ORIGINS adds
    # the public hostname when a reverse proxy (TLS + auth) fronts the server; the bind is 0.0.0.0
    # so a container port map or an SSH tunnel can reach it.
    origins = [f"localhost:{port}", f"127.0.0.1:{port}"]
    origins += [o.strip() for o in os.environ.get("DESKBOARD_ORIGINS", "").split(",") if o.strip()]
    server = pn.serve(make, port=port, address="0.0.0.0", show=show, title="deskboard", autoreload=False, start=False,
                      websocket_origin=origins)
    server.io_loop.add_callback(start_tasks)
    server.io_loop.start()
