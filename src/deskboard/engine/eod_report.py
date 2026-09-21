"""End-of-day report from a state directory (feeds/statefiles.py): one Markdown + one JSON per
trading day, built from the ledger, marks, funnel and regime rows. Same format every day so
days are comparable and the historical trade log is one click away.

    report = build_eod(files, "2026-06-15", reference)
    write_eod(report, out_dir)          # eod_2026-06-15.md / .json
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from ..feeds.statefiles import StateFiles


def _f(x, nd=2):
    try:
        v = float(x)
        return None if math.isnan(v) else round(v, nd)
    except (TypeError, ValueError):
        return None


def build_eod(files: StateFiles, day: str, reference: dict | None = None) -> dict[str, Any]:
    d = pd.Timestamp(day).normalize()
    eq = files.daily_equity()
    row = eq[eq["date"] == d]
    day_pnl = _f(row["daily_pnl"].iloc[0]) if not row.empty else None
    equity = _f(row["equity"].iloc[0]) if not row.empty else None
    spot = _f(row["spot"].iloc[0]) if not row.empty else None
    spot_ret = _f(row["spot_ret"].iloc[0], 4) if not row.empty else None
    peak = float(eq[eq["date"] <= d]["equity"].cummax().iloc[-1]) if not row.empty else None
    dd = _f(equity - peak) if equity is not None and peak is not None else None

    fq = files.fills_quality()
    fills_today = fq[fq["date"] == day] if not fq.empty else pd.DataFrame()
    trades = files.trades()
    opened = trades[pd.to_datetime(trades["opened"]).dt.normalize() == d] if not trades.empty else pd.DataFrame()
    closed = trades[pd.to_datetime(trades["closed"]).dt.normalize() == d] if not trades.empty else pd.DataFrame()

    fun = files.funnel()
    fun_today = fun[fun["qt"].dt.normalize() == d] if not fun.empty else pd.DataFrame()
    funnel = {}
    if not fun_today.empty:
        for sl, g in fun_today.groupby("sleeve"):
            funnel[sl] = dict(candidates=int(g["candidates"].sum()), after_dedupe=int(g["after_dedupe"].sum()),
                              tickets=int(g["tickets"].sum()), attempted=int(g["attempted"].sum()),
                              filled=int(g["filled"].sum()),
                              blocked=sorted({str(b) for b in g["blocked"].dropna() if b}))

    reg = files.regime()
    reg_today = reg[reg["qt"].dt.normalize() == d] if not reg.empty else pd.DataFrame()
    regime = {}
    if not reg_today.empty:
        last = reg_today.sort_values("qt").iloc[-1]
        for k in ("spot", "rsig", "atm_iv", "term_slope_front", "term_slope_back", "skew_25", "rr10",
                  "gex_bn_per_pct", "dex_m_shares", "universe"):
            if k in last and last[k] == last[k]:
                regime[k] = _f(last[k], 4)
        regime["snaps"] = int(len(reg_today))

    m = files.marks()
    eod_pos = []
    if not m.empty:
        mt = m[(m["qt"].dt.normalize() == d) & (m["row"] == "position")]
        if not mt.empty:
            last_qt = mt["qt"].max()
            for _, r in mt[mt["qt"] == last_qt].iterrows():
                eod_pos.append(dict(pos_id=r["pos_id"], sleeve=r["sleeve"], entry_qt=r["entry_qt"],
                                    snaps_held=int(r["snaps_held"]), credit=_f(r["entry_net_credit"]),
                                    mark=_f(r["mark_net"]), unrealized=_f(r["unrealized"]), marked=bool(r["marked"])))

    ref = reference or {}
    pace = ref.get("net_pace_usd_per_bday")
    sigma = ref.get("daily_sigma_usd")
    n_days = int(len(eq[eq["date"] <= d])) if not eq.empty else 0
    expected = _f(pace * n_days) if pace and n_days else None
    z = _f((equity - expected) / (sigma * math.sqrt(n_days)), 2) if (equity is not None and expected is not None and sigma and n_days) else None

    exec_summary = {}
    if not fills_today.empty:
        exec_summary = dict(n=int(len(fills_today)),
                            slip_vs_cross_c_mean=_f(fills_today["slip_vs_cross_c"].mean(), 1),
                            slip_vs_mid_c_mean=_f(fills_today["slip_vs_mid_c"].mean(), 1),
                            worst_vs_cross_c=_f(fills_today["slip_vs_cross_c"].max(), 1),
                            latency_ms_median=_f(fills_today["latency_ms"].median(), 0) if fills_today["latency_ms"].notna().any() else None,
                            outside_2c=int((fills_today["slip_vs_cross_c"] > 2.0).sum()))

    return dict(
        date=day, equity=equity, day_pnl=day_pnl, drawdown_from_peak=dd, spot=spot, spot_ret=spot_ret,
        n_days=n_days, expected_equity=expected, z_vs_expected=z,
        realized_today=_f(closed["realized"].sum()) if not closed.empty else 0.0,
        n_open_eod=len(eod_pos), positions=eod_pos,
        opened=[_trade_row(t) for _, t in opened.iterrows()],
        closed=[_trade_row(t) for _, t in closed.iterrows()],
        fills=[_fill_row(f) for _, f in fills_today.iterrows()],
        execution=exec_summary, funnel=funnel, regime=regime,
        source=("bot" if (not row.empty and row["source"].iloc[0] == "bot") else "reconstructed" if not row.empty else "none"),
    )


def _trade_row(t) -> dict:
    return dict(ticket_id=t["ticket_id"], sleeve=t["sleeve"], legs=t["legs"], expiry=t["expiry"],
                opened=str(t["opened"])[:16], closed=(str(t["closed"])[:16] if pd.notna(t["closed"]) else None),
                credit=_f(t["credit"]), close_debit=_f(t["close_debit"]), commission=_f(t["commission"]),
                realized=_f(t["realized"]), open_note=t.get("open_note", ""), close_note=t.get("close_note", ""))


def _fill_row(f) -> dict:
    return dict(time=str(f["ts"])[11:16], ticket_id=f["ticket_id"], sleeve=f["sleeve"], action=f["action"], legs=f["legs"],
                net_mid=_f(f["net_mid"], 3), net_cross=_f(f["net_cross"], 3), limit=_f(f["limit"], 3), fill=_f(f["fill"], 3),
                slip_vs_mid_c=_f(f["slip_vs_mid_c"], 1), slip_vs_cross_c=_f(f["slip_vs_cross_c"], 1),
                latency_ms=_f(f["latency_ms"], 0), note=f.get("note", ""))


def render_md(r: dict[str, Any]) -> str:
    def money(x):
        return "—" if x is None else f"{x:+,.0f}"
    L = [f"# EOD report — {r['date']}", ""]
    L.append(f"**Equity ${money(r['equity'])}** · day **${money(r['day_pnl'])}** · realized today ${money(r['realized_today'])} · "
             f"drawdown from peak ${money(r['drawdown_from_peak'])} · spot {r['spot'] if r['spot'] is not None else '—'} "
             f"({(r['spot_ret'] or 0) * 100:+.2f}%) · open {r['n_open_eod']} · marks: {r['source']}")
    if r.get("expected_equity") is not None and r.get("z_vs_expected") is not None:
        L.append(f"vs expected pace after {r['n_days']} bdays: expected ${money(r['expected_equity'])}, "
                 f"z = {r['z_vs_expected']:+.2f}σ (live vs expected pace, MTM-daily, net of commissions)")
    elif r.get("equity") is None:
        L.append("_no EOD mark for this day yet (the bot writes marks at its EOD snap; earlier days come from history.csv)_")
    L += ["", "## Trades opened"]
    L += _table(r["opened"], ["ticket_id", "sleeve", "legs", "expiry", "opened", "credit", "open_note"]) or ["_none_"]
    L += ["", "## Trades closed"]
    L += _table(r["closed"], ["ticket_id", "sleeve", "legs", "opened", "closed", "credit", "close_debit", "commission", "realized"]) or ["_none_"]
    L += ["", "## Fills (vs decision NBBO; + = worse for us)"]
    L += _table(r["fills"], ["time", "ticket_id", "action", "legs", "net_mid", "net_cross", "limit", "fill",
                             "slip_vs_mid_c", "slip_vs_cross_c", "latency_ms"]) or ["_none_"]
    ex = r.get("execution") or {}
    if ex:
        L.append(f"\n{ex['n']} fills · mean slip vs cross **{ex['slip_vs_cross_c_mean']:+.1f}¢** · vs mid {ex['slip_vs_mid_c_mean']:+.1f}¢ · "
                 f"worst vs cross {ex['worst_vs_cross_c']:+.1f}¢ · outside +2¢: {ex['outside_2c']}"
                 + (f" · median latency {ex['latency_ms_median']:.0f} ms" if ex.get("latency_ms_median") is not None else ""))
    L += ["", "## Entry funnel"]
    fun = r.get("funnel") or {}
    if fun:
        L += _table([dict(sleeve=k, **v) for k, v in fun.items()],
                    ["sleeve", "candidates", "after_dedupe", "tickets", "attempted", "filled", "blocked"])
    else:
        L.append("_no funnel rows for this day_")
    L += ["", "## Regime (last snap)"]
    reg = r.get("regime") or {}
    if reg:
        L.append(" · ".join(f"{k} {v}" for k, v in reg.items()))
    else:
        L.append("_no regime rows_")
    L += ["", "## Positions at EOD"]
    L += _table(r["positions"], ["pos_id", "sleeve", "entry_qt", "snaps_held", "credit", "mark", "unrealized"]) or ["_none_"]
    return "\n".join(L) + "\n"


def _table(rows: list[dict], cols: list[str]) -> list[str]:
    if not rows:
        return []
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |")
    return out


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}" if abs(v) < 100 else f"{v:,.0f}"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "—"
    return str(v).replace("|", "/")


def write_eod(report: dict[str, Any], out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = out / f"eod_{report['date']}.md"
    js = out / f"eod_{report['date']}.json"
    md.write_text(render_md(report))
    js.write_text(json.dumps(report, indent=1, default=str))
    return md, js


def report_days(files: StateFiles) -> list[str]:
    """Every day the book has a mark or a fill for."""
    eq = files.daily_equity()
    days = set(eq["date"].dt.strftime("%Y-%m-%d")) if not eq.empty else set()
    led = files.ledger()
    if not led.empty:
        days |= set(led["date"])
    return sorted(days)
