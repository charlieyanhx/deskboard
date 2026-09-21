"""Strategy pages driven by a state directory (feeds/statefiles.py): Pace, Grid, Regime,
Metrics, Execution, Reports.

Each builder returns ``(panel, refresh)``; ``refresh`` re-reads the files (cheap, they are
small) and repaints. ``build_state_tabs`` wraps them into ``build(extra_tabs=…)`` shape. The
reference (expected pace, daily σ, per-spread P&L-vs-return bands, a regime history) comes from
``reference.json`` / ``regime_history.*`` in the same directory — a real desk puts its
backtest's numbers there; the demo ships synthetic ones. Nothing here influences a trade.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import panel as pn
from bokeh.models import ColumnDataSource, HoverTool, Span
from bokeh.plotting import figure

from ..engine.eod_report import build_eod, render_md, report_days, write_eod
from ..feeds.statefiles import StateFiles
from ..feeds.synth_state import load_reference

BLUE, GREEN, RED, GREY, AMBER = "#2458a6", "#2a7a5a", "#b23b3b", "#8a8a8a", "#c98a1b"


# ----------------------------------------------------------------------------- Pace
def pace_page(files: StateFiles, bands: dict):
    pace = float(bands.get("net_pace_usd_per_bday", 0.0) or 0.0)
    sigma = float(bands.get("daily_sigma_usd", 0.0) or 0.0)
    by_ks = bands.get("by_ks", [])
    src_pace = ColumnDataSource(dict(date=[], equity=[], exp=[], hi1=[], lo1=[], hi2=[], lo2=[]))
    f1 = figure(height=260, sizing_mode="stretch_width", x_axis_type="datetime", toolbar_location=None,
                title="equity vs expected pace ($) — cones ±1σ/±2σ · √n from the reference daily σ")
    f1.varea("date", "lo2", "hi2", source=src_pace, color=BLUE, alpha=0.08)
    f1.varea("date", "lo1", "hi1", source=src_pace, color=BLUE, alpha=0.15)
    f1.line("date", "exp", source=src_pace, color=GREY, line_dash="dashed", legend_label="expected pace")
    f1.line("date", "equity", source=src_pace, color=BLUE, line_width=2, legend_label="book")
    f1.legend.location = "top_left"

    src_sc = ColumnDataSource(dict(ret=[], pnl=[], date=[], n=[], exp=[], tot=[]))
    src_ln = ColumnDataSource(dict(ret=[], lo=[], hi=[], mid=[]))
    f2 = figure(height=300, sizing_mode="stretch_width", toolbar_location=None,
                title="daily P&L PER OPEN SPREAD vs underlying return — dots: live days · band: reference p10–p90 per spread at today's spot")
    f2.varea("ret", "lo", "hi", source=src_ln, color=GREY, alpha=0.18)
    f2.line("ret", "mid", source=src_ln, color=GREY, line_dash="dashed")
    f2.scatter("ret", "pnl", source=src_sc, size=9, color=BLUE, alpha=0.85)
    f2.add_tools(HoverTool(tooltips=[("date", "@date"), ("underlying", "@ret{0.00%}"), ("book P&L", "@tot{$0,0}"), ("open", "@n"),
                                     ("per spread", "@pnl{$0,0}"), ("reference median/spread", "@exp{$0,0}")]))
    f2.xaxis.axis_label, f2.yaxis.axis_label = "underlying daily return", "P&L per open spread ($)"

    src_fun = ColumnDataSource(dict(date=[], candidates=[], after_dedupe=[], tickets=[], filled=[]))
    f3 = figure(height=240, sizing_mode="stretch_width", x_axis_type="datetime", toolbar_location=None,
                title="entry funnel per day: candidates → after dedupe → tickets → filled")
    w = 0.18 * 86400e3
    f3.vbar(x="date", top="candidates", width=w * 4, source=src_fun, color=GREY, alpha=0.35, legend_label="candidates")
    f3.vbar(x="date", top="after_dedupe", width=w * 3, source=src_fun, color=BLUE, alpha=0.45, legend_label="after dedupe")
    f3.vbar(x="date", top="tickets", width=w * 2, source=src_fun, color=AMBER, alpha=0.8, legend_label="tickets")
    f3.vbar(x="date", top="filled", width=w, source=src_fun, color=GREEN, legend_label="filled")
    f3.legend.location = "top_left"
    from bokeh.models import Label
    f3_note = Label(x=10, y=10, x_units="screen", y_units="screen", text="", text_color=GREY, text_font_size="11pt")
    f3.add_layout(f3_note)
    md = pn.pane.Markdown("")

    def refresh():
        eq = files.daily_equity()
        if eq.empty:
            md.object = "**pace** no marks yet"
            return
        n = np.arange(1, len(eq) + 1)
        exp = pace * n
        s = sigma * np.sqrt(n)
        src_pace.data = dict(date=eq["date"], equity=eq["equity"], exp=exp, hi1=exp + s, lo1=exp - s, hi2=exp + 2 * s, lo2=exp - 2 * s)
        z = (eq["equity"].iloc[-1] - exp[-1]) / s[-1] if sigma else float("nan")
        d = eq.dropna(subset=["spot_ret"]).copy()
        d["n"] = files.open_count_by_day(d["date"]).to_numpy()
        d["per"] = d["daily_pnl"] / d["n"].where(d["n"] > 0, np.nan)
        # reference expectation per spread: the all-moneyness regression, spot-scaled
        if by_ks:
            wsum = sum(b["n"] for b in by_ks) or 1
            slope = sum(b["slope_per_pct_per100spot"] * b["n"] for b in by_ks) / wsum
            icpt = sum(b["intercept_per100spot"] * b["n"] for b in by_ks) / wsum
            p10 = sum(b["deq_p10"] * b["n"] for b in by_ks) / wsum
            p90 = sum(b["deq_p90"] * b["n"] for b in by_ks) / wsum
            p50 = sum(b["deq_p50"] * b["n"] for b in by_ks) / wsum
            d["exp"] = (icpt + slope * d["spot_ret"] * 100) * d["spot"] / 100
            grid = np.linspace(-0.03, 0.03, 61)
            spot = float(d["spot"].iloc[-1])
            dS = grid * spot
            src_ln.data = dict(ret=grid, mid=p50 * dS * 100, lo=np.where(dS < 0, p90, p10) * dS * 100,
                               hi=np.where(dS < 0, p10, p90) * dS * 100)
        else:
            d["exp"] = np.nan
        dd = d.dropna(subset=["per"])
        src_sc.data = dict(ret=dd["spot_ret"], pnl=dd["per"], tot=dd["daily_pnl"], date=dd["date"].dt.strftime("%Y-%m-%d"),
                           n=dd["n"], exp=dd["exp"])
        fun = files.funnel()
        if not fun.empty:
            g = fun.assign(date=fun["qt"].dt.normalize()).groupby("date")[["candidates", "after_dedupe", "tickets", "attempted", "filled"]].sum()
            src_fun.data = dict(date=g.index, **{c: g[c] for c in ("candidates", "after_dedupe", "tickets", "filled")})
            live = fun[fun["source"] == "bot"]
            fr = (live["filled"].sum() / live["attempted"].sum()) if not live.empty and live["attempted"].sum() else float("nan")
            n_rec = int((fun["source"] != "bot").sum())
            f3_note.text = ""
            fun_txt = (f"funnel: {int(g['filled'].sum())}/{int(g['attempted'].sum())} attempted filled, "
                       f"{n_rec} snap rows reconstructed from logs" if n_rec else f"funnel: {int(g['filled'].sum())}/{int(g['attempted'].sum())} filled")
        else:
            fr = float("nan")
            fun_txt = "funnel: no rows yet"
            f3_note.text = "no funnel rows yet — the bot writes one per entry snap"
        fr_txt = f"{fr:.0%}" if fr == fr else "n/a"
        md.object = (f"**pace** equity **${eq['equity'].iloc[-1]:,.0f}** after {len(eq)} bdays · expected ${exp[-1]:,.0f} "
                     f"± ${s[-1]:,.0f} → **z = {z:+.2f}σ** · {fun_txt} · fill rate (bot rows) {fr_txt}"
                     f" · basis: MTM-daily mid marks, net of commissions; reference = {bands.get('source', 'reference.json')}")

    page = pn.Column(md, pn.pane.Bokeh(f1), pn.pane.Bokeh(f2), pn.pane.Bokeh(f3), pn.pane.Markdown(
        "The pace cone is the only honest Sharpe statement on a short sample: z is (equity − pace·n)/(σ·√n), with the "
        "pace and σ taken from the reference (a backtest), not fitted to the live days. The scatter answers 'is the held "
        "book behaving like the reference' — a day inside the band is the strategy doing what it does; a day outside it is "
        "a marks, Greeks or inventory question before it is a strategy question. The funnel is breadth: every candidate "
        "not filled is a whole trade's expected value, usually far more than any execution improvement."),
        sizing_mode="stretch_width")
    return page, refresh


# ----------------------------------------------------------------------------- Grid
def grid_page(files: StateFiles):
    md = pn.pane.Markdown("")
    snap_sel = pn.widgets.Select(name="surface snap", options=[], width=220)
    right_sel = pn.widgets.RadioButtonGroup(name="side", options=["P", "C"], value="P")
    grid_tbl = pn.widgets.Tabulator(pd.DataFrame(), show_index=True, layout="fit_data_table", height=520, disabled=True,
                                    pagination=None)
    src_g = ColumnDataSource(dict(strike=[], gex=[], dex=[], color=[]))
    fg = figure(height=260, sizing_mode="stretch_width", toolbar_location=None,
                title="dealer GEX by strike ($bn per 1 %, calls +, puts −) and DEX (M shares) — from the persisted chain")
    fg.vbar(x="strike", top="gex", width=0.8, source=src_g, color="color", legend_label="GEX")
    fg.line("strike", "dex", source=src_g, color=AMBER, line_width=2, legend_label="DEX", y_range_name="dex")
    from bokeh.models import LinearAxis, Range1d
    fg.extra_y_ranges = {"dex": Range1d(start=-1, end=1)}
    fg.add_layout(LinearAxis(y_range_name="dex", axis_label="DEX (M sh)"), "right")
    fg.legend.location = "top_left"
    spot_span = Span(location=0, dimension="height", line_color=RED, line_dash="dashed")
    fg.add_layout(spot_span)

    def _positions_by_key():
        out = {}
        for p in files.positions():
            for lg in p["legs"]:
                if lg.get("sec_type", "OPT") != "OPT":
                    continue
                k = (str(lg["expiration"])[:8], float(lg["strike"]), str(lg["right"])[0])
                q = float(lg["quantity"]) * (1 if lg["side"] == "BUY" else -1)
                out[k] = out.get(k, 0.0) + q
        return out

    def repaint(*_):
        days = files.surface_days()
        if not days:
            md.object = "**grid** no surfaces persisted yet"
            return
        day = days[-1]
        snaps = files.surface_snaps(day)
        opts = [f"{day} {s[:2]}:{s[2:]}" for s in snaps]
        if snap_sel.options != opts:
            snap_sel.options = opts
            snap_sel.value = opts[-1]
        chosen = snap_sel.value or opts[-1]
        hhmm = chosen[-5:].replace(":", "")
        df = files.surface(day, hhmm)
        if df is None or df.empty:
            return
        spot = float(df["spot"].iloc[0])
        df["exp"] = df["expiration"].astype(str).str.replace("-", "").str[:8]
        held = _positions_by_key()
        side = right_sel.value
        sub = df[df["option_type"].astype(str).str.upper().str.startswith(side)].copy()
        sub = sub[(sub["strike"] >= spot * 0.88) & (sub["strike"] <= spot * 1.10)]
        piv = sub.pivot_table(index="strike", columns="exp", values="delta", aggfunc="first").sort_index(ascending=False)
        cells = pd.DataFrame(index=piv.index, columns=piv.columns, dtype=object)
        for k in piv.index:
            for e in piv.columns:
                q = held.get((e, float(k), side), 0.0)
                dl = piv.loc[k, e]
                txt = f"{dl:+.2f}" if dl == dl else ""
                if q:
                    txt = f"{'▲' if q > 0 else '▼'}{abs(q):g} {txt}"
                cells.loc[k, e] = txt
        cells.index = [f"{k:g}{' ◀ spot' if abs(k - spot) < 0.5 else ''}" for k in cells.index]
        grid_tbl.value = cells
        # GEX / DEX by strike from the whole chain (both sides)
        has_oi = "open_interest" in df.columns and float(pd.to_numeric(df["open_interest"], errors="coerce").fillna(0).sum()) > 0
        if has_oi:
            oi = pd.to_numeric(df["open_interest"], errors="coerce").fillna(0)
            isc = df["option_type"].astype(str).str.upper().str.startswith("C")
            g = (pd.to_numeric(df["gamma"], errors="coerce").fillna(0) * oi * 100 * spot ** 2 / 100 * np.where(isc, 1, -1)) / 1e9
            dx = (pd.to_numeric(df["delta"], errors="coerce").fillna(0) * oi * 100) / 1e6
            agg = pd.DataFrame({"strike": df["strike"], "gex": g, "dex": dx}).groupby("strike").sum()
            agg = agg[(agg.index >= spot * 0.9) & (agg.index <= spot * 1.1)]
            src_g.data = dict(strike=agg.index, gex=agg["gex"], dex=agg["dex"], color=[GREEN if v >= 0 else RED for v in agg["gex"]])
            lim = float(max(agg["dex"].abs().max(), 1e-6))
            fg.extra_y_ranges["dex"].start, fg.extra_y_ranges["dex"].end = -lim * 1.1, lim * 1.1
            tot = f"GEX Σ {agg['gex'].sum():+.2f} $bn/1% · DEX Σ {agg['dex'].sum():+.1f} M sh"
        else:
            src_g.data = dict(strike=[], gex=[], dex=[], color=[])
            tot = "GEX/DEX unavailable: this chain carries no open interest"
        spot_span.location = spot
        n_held = sum(1 for k in held if k[2] == side)
        md.object = (f"**grid** {chosen} · spot **{spot:.2f}** · {len(df):,} contracts on the surface · {n_held} held {side} legs "
                     f"(▲ long ▼ short, number = contracts; cell value = delta at that snap) · {tot}")

    snap_sel.param.watch(repaint, "value")
    right_sel.param.watch(repaint, "value")
    page = pn.Column(md, pn.Row(snap_sel, right_sel), grid_tbl, pn.pane.Bokeh(fg), pn.pane.Markdown(
        "Rows are strikes (spot marked), columns expiries; held legs are overlaid on the delta the strategy saw at that snap. "
        "GEX uses the dealer-short-puts / long-calls sign convention: positive bars are strikes where dealers are long gamma "
        "(they damp moves), negative where they are short (they chase). DEX is signed open-interest delta in millions of shares."),
        sizing_mode="stretch_width")
    return page, repaint


# ----------------------------------------------------------------------------- Regime
REGIME_VARS = [("atm_iv", "ATM IV (30d)", 100, "vol pts"), ("term_slope_front", "term slope front−30d", 100, "vol pts"),
               ("term_slope_back", "term slope back−30d", 100, "vol pts"), ("skew_25", "25Δ skew (put−call)", 100, "vol pts"),
               ("gex_bn_per_pct", "GEX", 1, "$bn / 1%"), ("dex_m_shares", "DEX", 1, "M shares")]


def regime_page(files: StateFiles, hist: pd.DataFrame | None):
    md = pn.pane.Markdown("")
    figs, srcs, spans = [], [], []
    for _key, label, _scale, unit in REGIME_VARS:
        s = ColumnDataSource(dict(left=[], right=[], top=[]))
        f = figure(height=190, sizing_mode="stretch_width", toolbar_location=None, title=f"{label} — history vs today ({unit})")
        f.quad(left="left", right="right", top="top", bottom=0, source=s, color=BLUE, alpha=0.35)
        sp = Span(location=0, dimension="height", line_color=RED, line_width=2)
        f.add_layout(sp)
        figs.append(f)
        srcs.append(s)
        spans.append(sp)
    src_ts = ColumnDataSource(dict(qt=[], atm=[], slope=[], universe=[]))
    ft = figure(height=220, sizing_mode="stretch_width", x_axis_type="datetime", toolbar_location=None,
                title="the strategy's own regime rows: ATM IV (vol pts, left) · term slope front (vol pts, left) · universe (right)")
    ft.line("qt", "atm", source=src_ts, color=BLUE, legend_label="ATM IV")
    ft.line("qt", "slope", source=src_ts, color=AMBER, legend_label="slope front")
    from bokeh.models import LinearAxis, Range1d
    ft.extra_y_ranges = {"u": Range1d(start=0, end=500)}
    ft.add_layout(LinearAxis(y_range_name="u", axis_label="universe"), "right")
    ft.line("qt", "universe", source=src_ts, color=GREY, y_range_name="u", legend_label="universe")
    ft.legend.location = "top_left"
    tbl = pn.widgets.Tabulator(pd.DataFrame(columns=["metric", "today", "hist median", "percentile", "unit"]), show_index=False,
                               layout="fit_data_table", height=230, disabled=True)

    def refresh():
        reg = files.regime()
        rows = []
        today = reg.sort_values("qt").iloc[-1] if not reg.empty else None
        for (key, label, scale, unit), _f, s, sp in zip(REGIME_VARS, figs, srcs, spans, strict=True):
            hv = None
            if hist is not None and key in hist.columns:
                hv = pd.to_numeric(hist[key], errors="coerce").dropna() * scale
                if len(hv):
                    cnt, edges = np.histogram(hv, bins=60)
                    s.data = dict(left=edges[:-1], right=edges[1:], top=cnt)
            tv = None
            if today is not None and key in today and today[key] == today[key] and today[key] is not None:
                tv = float(today[key]) * scale
                sp.location = tv
                sp.visible = True
            else:
                sp.visible = False
            pct = float((hv < tv).mean() * 100) if (hv is not None and len(hv) and tv is not None) else None
            rows.append(dict(metric=label, today=(round(tv, 2) if tv is not None else None),
                             **{"hist median": (round(float(hv.median()), 2) if hv is not None and len(hv) else None),
                                "percentile": (round(pct, 0) if pct is not None else None)}, unit=unit))
        tbl.value = pd.DataFrame(rows)
        if not reg.empty:
            r = reg.sort_values("qt")
            def col(name, scale=1.0):
                return (pd.to_numeric(r[name], errors="coerce") * scale) if name in r else pd.Series(np.nan, index=r.index)
            src_ts.data = dict(qt=r["qt"], atm=col("atm_iv", 100), slope=col("term_slope_front", 100), universe=col("universe"))
            bw = "backwardation" if (today is not None and today.get("term_slope_front", 0) and today["term_slope_front"] > 0) else "contango"
            md.object = (f"**regime** last snap {today['qt']:%Y-%m-%d %H:%M} · spot {today.get('spot')} · "
                         f"universe {today.get('universe')} · front slope says **{bw}** — a label for reading the sample "
                         f"against the right part of the history, not a signal")
        else:
            md.object = "**regime** no regime rows yet"

    page = pn.Column(md, tbl, *[pn.pane.Bokeh(f) for f in figs], pn.pane.Bokeh(ft), pn.pane.Markdown(
        "History is regime_history.(csv|parquet) in the state directory, with the same column definitions the bot writes. "
        "The red line is the latest snap; the percentile is where today sits in that history."), sizing_mode="stretch_width")
    return page, refresh


# ----------------------------------------------------------------------------- Metrics
HORIZONS = {"1 week": 5, "2 weeks": 10, "1 month": 21, "3 months": 63, "all": 10 ** 6}


def metrics_page(files: StateFiles, bands: dict):
    sel = pn.widgets.Select(name="horizon", options=list(HORIZONS), value="all", width=160)
    tbl = pn.widgets.Tabulator(pd.DataFrame(columns=["metric", "value", "note"]), show_index=False, layout="fit_data_table",
                               height=520, disabled=True)
    md = pn.pane.Markdown("")
    pace = float(bands.get("net_pace_usd_per_bday", 0.0) or 0.0)
    sigma = float(bands.get("daily_sigma_usd", 0.0) or 0.0)

    def refresh(*_):
        eq = files.daily_equity()
        if eq.empty:
            md.object = "**metrics** no marks yet"
            return
        n = HORIZONS[sel.value]
        w = eq.tail(n).copy()
        base = float(eq["equity"].iloc[-n - 1]) if len(eq) > n else 0.0
        w["cum"] = w["equity"] - base
        pnl = w["daily_pnl"]
        days = len(w)
        trades = files.trades()
        t = trades[pd.to_datetime(trades["closed"]) >= w["date"].iloc[0]].dropna(subset=["realized"]) if not trades.empty else pd.DataFrame()
        opened = trades[pd.to_datetime(trades["opened"]) >= w["date"].iloc[0]] if not trades.empty else pd.DataFrame()
        cum = w["cum"]
        dd = (cum - cum.cummax()).min()
        sh = (pnl.mean() / pnl.std() * math.sqrt(252)) if days > 2 and pnl.std() > 0 else float("nan")
        se = math.sqrt((1 + sh ** 2 / 2) * 252 / days) if days > 2 and sh == sh else float("nan")
        corr = pnl.corr(w["spot_ret"]) if days > 2 else float("nan")
        exp = pace * days
        z = (cum.iloc[-1] - exp) / (sigma * math.sqrt(days)) if sigma else float("nan")
        rows = [
            ("P&L over horizon", f"${cum.iloc[-1]:+,.0f}", f"{days} bdays, MTM-daily mid marks, net of commissions"),
            ("realized (closed in window)", f"${t['realized'].sum():+,.0f}" if not t.empty else "$0", f"{len(t)} closes"),
            ("trades opened / closed", f"{len(opened)} / {len(t)}", ""),
            ("hit rate (closed)", f"{(t['realized'] > 0).mean():.0%}" if not t.empty else "—", ""),
            ("avg realized per close", f"${t['realized'].mean():+,.0f}" if not t.empty else "—",
             ""),
            ("best / worst day", f"${pnl.max():+,.0f} / ${pnl.min():+,.0f}", ""),
            ("daily σ", f"${pnl.std():,.0f}", f"reference ${sigma:,.0f}"),
            ("max drawdown (window)", f"${dd:,.0f}", ""),
            ("Sharpe (annualised)", f"{sh:.2f}" if sh == sh else "—", f"± {se:.1f} (1σ) on {days} days — do not quote below ~60 days"),
            ("corr(daily P&L, underlying)", f"{corr:+.2f}" if corr == corr else "—", ""),
            ("underlying over horizon", f"{(w['spot'].iloc[-1] / w['spot'].iloc[0] - 1):+.2%}" if days > 1 else "—", ""),
            ("vs expected pace", f"z = {z:+.2f}σ" if z == z else "—", f"expected ${exp:+,.0f} ± ${sigma * math.sqrt(days):,.0f}"),
        ]
        tbl.value = pd.DataFrame(rows, columns=["metric", "value", "note"])
        md.object = f"**metrics** {sel.value} · {w['date'].iloc[0]:%Y-%m-%d} → {w['date'].iloc[-1]:%Y-%m-%d}"

    sel.param.watch(refresh, "value")
    page = pn.Column(pn.Row(sel, md), tbl, pn.pane.Markdown(
        "Every number is labelled with its basis. The Sharpe row carries its standard error so a two-week Sharpe reads as what it is."),
        sizing_mode="stretch_width")
    return page, refresh


# ----------------------------------------------------------------------------- Execution
def execution_page(files: StateFiles):
    md = pn.pane.Markdown("")
    src = ColumnDataSource(dict(ts=[], slip=[], color=[], label=[], lat=[]))
    f = figure(height=280, sizing_mode="stretch_width", x_axis_type="datetime", toolbar_location=None,
               title="fill vs decision cross (¢, + = worse) — every fill; band = G2 bar ±2¢")
    f.scatter("ts", "slip", source=src, size=10, color="color", alpha=0.9)
    for y, c in ((2, RED), (0, GREY), (-2, GREEN)):
        f.add_layout(Span(location=y, dimension="width", line_color=c, line_dash="dashed"))
    f.add_tools(HoverTool(tooltips=[("fill", "@label"), ("slip", "@slip{0.0}¢"), ("latency", "@lat ms")]))
    tbl = pn.widgets.Tabulator(pd.DataFrame(), show_index=False, layout="fit_data_table", height=360, disabled=True)

    def refresh():
        q = files.fills_quality()
        if q.empty:
            md.object = "**execution** no fills in the ledger"
            return
        src.data = dict(ts=q["ts"], slip=q["slip_vs_cross_c"], lat=q["latency_ms"].fillna(-1),
                        color=[RED if v > 2 else (GREEN if v <= 0 else AMBER) for v in q["slip_vs_cross_c"]],
                        label=q["ticket_id"] + " " + q["action"] + " " + q["legs"])
        recent = q.tail(20)
        tbl.value = q[["ts", "ticket_id", "sleeve", "action", "legs", "net_mid", "net_cross", "limit", "fill",
                       "slip_vs_mid_c", "slip_vs_cross_c", "latency_ms"]].iloc[::-1]
        lat = f" · median latency {q['latency_ms'].median():.0f} ms" if q["latency_ms"].notna().any() else ""
        md.object = (f"**execution** {len(q)} fills · mean slip vs cross **{q['slip_vs_cross_c'].mean():+.1f}¢** "
                     f"(last 20: {recent['slip_vs_cross_c'].mean():+.1f}¢) · vs mid {q['slip_vs_mid_c'].mean():+.1f}¢ · "
                     f"outside +2¢: {int((q['slip_vs_cross_c'] > 2).sum())}{lat}")

    page = pn.Column(md, pn.pane.Bokeh(f), tbl, pn.pane.Markdown(
        "Reference is the NBBO recorded on each leg at submission (the decision quote). A fill outside the recorded "
        "quote means the reference was stale when the order went out — a measurement problem before an execution one."), sizing_mode="stretch_width")
    return page, refresh


# ----------------------------------------------------------------------------- Reports + trade log
def reports_page(files: StateFiles, bands: dict, reports_dir: Path | None):
    day_sel = pn.widgets.Select(name="EOD report", options=[], width=200)
    write_btn = pn.widgets.Button(name="save report to disk", button_type="primary", width=180)
    saved_md = pn.pane.Markdown("")
    report_md = pn.pane.Markdown("", sizing_mode="stretch_width")
    log_tbl = pn.widgets.Tabulator(pd.DataFrame(), show_index=False, layout="fit_data_table", height=320, disabled=True,
                                   selectable=1)
    detail_md = pn.pane.Markdown("_click a trade for its fills, quotes and notes_")

    def show_day(*_):
        if not day_sel.value:
            return
        report_md.object = render_md(build_eod(files, day_sel.value, bands))

    def save(*_):
        if not day_sel.value or reports_dir is None:
            saved_md.object = "_no reports dir configured_"
            return
        md, js = write_eod(build_eod(files, day_sel.value, bands), reports_dir)
        saved_md.object = f"saved `{md.name}` and `{js.name}` in `{reports_dir}`"

    def on_select(event):
        sel = event.new if isinstance(event.new, list) else []
        t = files.trades()
        if t.empty or not sel or sel[0] >= len(t):
            return
        r = t.iloc[sel[0]]
        q = files.fills_quality()
        q = q[q["ticket_id"] == r["ticket_id"]]
        lines = [f"### {r['ticket_id']} — {r['sleeve']} {r['legs']} exp {r['expiry']}",
                 f"opened {r['opened']} · closed {r['closed'] if pd.notna(r['closed']) else 'open'} · credit ${r['credit']:,.0f}"
                 + (f" · close debit ${r['close_debit']:,.0f} · commissions ${r['commission']:.2f} · **realized ${r['realized']:+,.0f}**"
                    if r["realized"] is not None and r["realized"] == r["realized"] else ""), ""]
        for _, fq in q.iterrows():
            lim = f"{fq['limit']:+.3f}" if fq["limit"] is not None and fq["limit"] == fq["limit"] else "—"
            lat = f" · {fq['latency_ms']:.0f} ms" if fq["latency_ms"] is not None and fq["latency_ms"] == fq["latency_ms"] else ""
            lines.append(f"- **{fq['action']}** {fq['ts']:%Y-%m-%d %H:%M} · mid {fq['net_mid']:+.3f} · cross {fq['net_cross']:+.3f} · "
                         f"limit {lim} · fill **{fq['fill']:+.3f}** · slip vs cross {fq['slip_vs_cross_c']:+.1f}¢{lat}"
                         f"\n  `{fq['note']}`")
        m = files.marks()
        if not m.empty:
            mp = m[(m["row"] == "position") & (m["pos_id"] == r["ticket_id"])].sort_values("qt")
            if not mp.empty:
                lines.append(f"\nmarks: {len(mp)} snaps · unrealized path min ${mp['unrealized'].min():+,.0f} / max ${mp['unrealized'].max():+,.0f} / last ${mp['unrealized'].iloc[-1]:+,.0f}")
        detail_md.object = "\n".join(lines)

    def refresh():
        t = files.trades()
        if not t.empty:
            log_tbl.value = t[["ticket_id", "sleeve", "legs", "expiry", "opened", "closed", "credit", "close_debit",
                               "commission", "realized"]]
        days = report_days(files)
        if days and day_sel.options != days:
            keep = day_sel.value
            day_sel.options = days
            day_sel.value = keep if keep in days else days[-1]
            show_day()

    day_sel.param.watch(show_day, "value")
    write_btn.on_click(save)
    log_tbl.param.watch(on_select, "selection")   # selection survives a disabled (read-only) table; on_click does not
    page = pn.Column(pn.Row(day_sel, write_btn, saved_md), report_md, pn.pane.Markdown("### Trade log (all days; click a row)"),
                     log_tbl, detail_md, sizing_mode="stretch_width")
    return page, refresh


def pnl_history_for_health(state_dir: str | Path, out_dir: str | Path | None) -> str | None:
    """Write the Health page's date,pnl,backtest CSV from the strategy's daily equity (backtest =
    the reference's net pace per bday). Returns the path, or None when there is nothing to write."""
    files = StateFiles(state_dir)
    bands, _ = load_reference(state_dir)
    out = Path(out_dir or state_dir) / "pnl_history.csv"
    try:
        p = files.write_pnl_history(out, bands.get("net_pace_usd_per_bday"))
    except OSError:
        return None
    return str(p) if p else None


def build_state_tabs(state_dir: str | Path, reports_dir: str | Path | None = None, names: dict | None = None):
    """``build(extra_tabs=…)`` entries for a state directory: [(name, panel, refresh), …]."""
    files = StateFiles(state_dir, names)
    bands, hist = load_reference(state_dir)
    pages = [("Pace", pace_page(files, bands)), ("Grid", grid_page(files)), ("Regime", regime_page(files, hist)),
             ("Metrics", metrics_page(files, bands)), ("Fills", execution_page(files)),
             ("Reports", reports_page(files, bands, Path(reports_dir) if reports_dir else None))]
    return [(name, p, r) for name, (p, r) in pages]
