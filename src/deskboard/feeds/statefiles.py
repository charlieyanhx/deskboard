"""Readers for a strategy's STATE DIRECTORY — the file contract between a trading bot and the desk.

A bot that writes these files (append-only JSONL, one row per snap) gets every page in
``ui.pages`` for free; the desk never needs the bot's code or its broker connection:

    positions.json      open positions: [{pos_id, sleeve, entry_qt, entry_net_credit, snaps_held,
                        legs: [{symbol, sec_type, expiration, strike, right, side, quantity,
                        avg_fill_price}]}]  (the same shape feeds/blotter.py accepts)
    ledger.jsonl        fills: {event: "fill", ts, date, sleeve, action: open|close, ticket_id,
                        intended_limit, fill_net, commission, note,
                        legs: [{side, sec_type, qty, bid, ask, fill, right, strike, expiry}]}
                        — bid/ask are the DECISION quotes at submission; fill_net / limits use the
                        signed net convention (+ debit, − credit); note may carry "latency_ms=…"
    marks.jsonl         per snap: one row per position {qt, row: "position", pos_id, sleeve, entry_qt,
                        snaps_held, entry_net_credit, mark_net, unrealized, marked, spot} and one
                        {qt, row: "book", n_open, unrealized, realized_today, spot, kind: snap|eod}
    funnel.jsonl        per entry snap and sleeve: {qt, sleeve, candidates, after_dedupe, tickets,
                        attempted, filled, cap_remaining, blocked}
    regime.jsonl        per snap: {qt, spot, universe, atm_iv, term_slope_front, term_slope_back,
                        skew_25, gex_bn_per_pct, dex_m_shares, …}  (any extra keys are kept)
    surfaces/YYYY-MM-DD/HHMM.parquet|csv   the chain the strategy saw: strike, expiration,
                        option_type, implied_volatility, delta, gamma, bid, ask, open_interest, spot
    history.csv         optional: date,spot,equity for days before the bot wrote marks (reconstructed)
    funnel_history.jsonl  optional: funnel rows reconstructed from logs (source=reconstructed;
                        a bot row for the same (qt, sleeve) wins)

File names are overridable through ``names`` so an existing bot need not rename anything.
Everything is read fresh on each call (the files are small); pages refresh on a slow timer.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_NAMES = {
    "positions": "positions.json", "ledger": "ledger.jsonl", "marks": "marks.jsonl", "funnel": "funnel.jsonl",
    "regime": "regime.jsonl", "surfaces": "surfaces", "history": "history.csv", "funnel_history": "funnel_history.jsonl",
}


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class StateFiles:
    def __init__(self, state_dir: str | Path, names: dict[str, str] | None = None):
        self.state_dir = Path(state_dir)
        self.names = {**DEFAULT_NAMES, **(names or {})}

    def _p(self, key: str) -> Path:
        return self.state_dir / self.names[key]

    # ---- raw ------------------------------------------------------------------
    def positions(self) -> list[dict]:
        p = self._p("positions")
        try:
            raw = json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        pos = raw.get("positions", raw) if isinstance(raw, dict) else raw
        return list(pos) if isinstance(pos, list) else list(pos.values())

    def ledger(self) -> pd.DataFrame:
        rows = [r for r in _jsonl(self._p("ledger")) if r.get("event") == "fill"]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["ts"])
        return df.sort_values("ts").reset_index(drop=True)

    def marks(self) -> pd.DataFrame:
        df = pd.DataFrame(_jsonl(self._p("marks")))
        if not df.empty:
            df["qt"] = pd.to_datetime(df["qt"])
        return df

    def funnel(self) -> pd.DataFrame:
        """Bot funnel rows, plus ``funnel_history`` (rows reconstructed from logs for snaps before
        the bot wrote them; each carries ``source``). The bot's rows win on a clash."""
        hist = _jsonl(self._p("funnel_history"))
        live = _jsonl(self._p("funnel"))
        rows = [dict(r, source=r.get("source", "reconstructed")) for r in hist] + [dict(r, source="bot") for r in live]
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["qt"] = pd.to_datetime(df["qt"])
        df["_rank"] = (df["source"] == "bot").astype(int)          # bot rows sort last → kept
        df = df.sort_values(["qt", "_rank"]).drop_duplicates(["qt", "sleeve"], keep="last").drop(columns="_rank")
        return df.reset_index(drop=True)

    def write_pnl_history(self, path: str | Path, backtest_per_day: float | None = None) -> Path | None:
        """date,pnl[,backtest] CSV for the Health page (CUSUM live vs backtest)."""
        eq = self.daily_equity()
        if eq.empty:
            return None
        out = pd.DataFrame({"date": eq["date"].dt.strftime("%Y-%m-%d"), "pnl": eq["daily_pnl"].round(2)})
        if backtest_per_day is not None:
            out["backtest"] = float(backtest_per_day)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(path, index=False)
        return path

    def regime(self) -> pd.DataFrame:
        df = pd.DataFrame(_jsonl(self._p("regime")))
        if not df.empty:
            df["qt"] = pd.to_datetime(df["qt"])
        return df

    def surface_days(self) -> list[str]:
        d = self._p("surfaces")
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []

    def surface_snaps(self, day: str) -> list[str]:
        d = self._p("surfaces") / day
        return sorted({p.stem for p in d.iterdir() if p.suffix in (".parquet", ".csv")}) if d.exists() else []

    def surface(self, day: str | None = None, hhmm: str | None = None) -> pd.DataFrame | None:
        days = self.surface_days()
        if not days:
            return None
        day = day or days[-1]
        snaps = self.surface_snaps(day)
        if not snaps:
            return None
        hhmm = hhmm or snaps[-1]
        base = self._p("surfaces") / day / hhmm
        try:
            if base.with_suffix(".parquet").exists():
                return pd.read_parquet(base.with_suffix(".parquet"))
            return pd.read_csv(base.with_suffix(".csv"))
        except Exception:  # noqa: BLE001
            return None

    # ---- derived ----------------------------------------------------------------
    def trades(self) -> pd.DataFrame:
        """One row per ticket: open fill, close fill (if any), credits, realized P&L."""
        led = self.ledger()
        if led.empty:
            return pd.DataFrame()
        opens = led[led["action"] == "open"].set_index("ticket_id")
        closes = led[led["action"] == "close"].set_index("ticket_id")
        rows = []
        for tid, o in opens.iterrows():
            legs = o["legs"]
            c = closes.loc[tid] if tid in closes.index else None
            credit = -float(o["fill_net"]) * 100.0
            debit = float(c["fill_net"]) * 100.0 if c is not None else None
            comm = float(o["commission"]) + (float(c["commission"]) if c is not None else 0.0)
            rows.append(dict(
                ticket_id=tid, sleeve=o["sleeve"], opened=o["ts"], closed=(c["ts"] if c is not None else pd.NaT),
                expiry=legs[0]["expiry"], short=next((g["strike"] for g in legs if g["side"] == "SELL"), None),
                wing=next((g["strike"] for g in legs if g["side"] == "BUY"), None), right=legs[0]["right"],
                legs=" + ".join(f"{g['side']} {g['strike']:g}{g['right']}" for g in legs),
                credit=round(credit, 2), close_debit=(round(debit, 2) if debit is not None else None),
                commission=round(comm, 2),
                realized=(round(credit - debit - comm, 2) if debit is not None else None),
                open_note=o.get("note", ""), close_note=(c.get("note", "") if c is not None else ""),
                open_limit=o.get("intended_limit"), open_fill=o.get("fill_net"),
                close_limit=(c.get("intended_limit") if c is not None else None),
                close_fill=(c.get("fill_net") if c is not None else None),
            ))
        return pd.DataFrame(rows).sort_values("opened", ascending=False).reset_index(drop=True)

    def fills_quality(self) -> pd.DataFrame:
        """Per fill: net fill vs the decision NBBO recorded on the legs (cents, + = worse)."""
        led = self.ledger()
        if led.empty:
            return pd.DataFrame()
        rows = []
        for _, f in led.iterrows():
            legs = f["legs"]
            sgn = [1.0 if g["side"] == "BUY" else -1.0 for g in legs]
            mid = sum(s * (g["bid"] + g["ask"]) / 2 for s, g in zip(sgn, legs, strict=True))
            cross = sum((g["ask"] if s > 0 else -g["bid"]) for s, g in zip(sgn, legs, strict=True))
            fill = float(f["fill_net"])
            note = str(f.get("note", "") or "")
            lat = None
            for tok in note.split():
                if tok.startswith("latency_ms="):
                    try:
                        lat = float(tok.split("=", 1)[1])
                    except ValueError:
                        pass
            # a fill whose decision quote was not taken at submission is measured against a stale
            # reference; the bot marks fresh ones in the note ("fresh=2/2"). Mixing the two bases
            # makes execution look free.
            fresh = "fresh=" in note
            rows.append(dict(ts=f["ts"], date=f["date"], ticket_id=f["ticket_id"], sleeve=f["sleeve"], action=f["action"],
                             fresh=fresh,
                             legs=" + ".join(f"{g['side']} {g['strike']:g}{g['right']}" for g in legs),
                             net_mid=round(mid, 3), net_cross=round(cross, 3), limit=f.get("intended_limit"), fill=fill,
                             slip_vs_mid_c=round((fill - mid) * 100, 1), slip_vs_cross_c=round((fill - cross) * 100, 1),
                             latency_ms=lat, note=note))
        return pd.DataFrame(rows)

    def daily_equity(self) -> pd.DataFrame:
        """date, equity (realized-to-date + EOD unrealized), daily_pnl, spot, spot_ret. Uses the
        bot's EOD ``book`` mark rows; days before the bot wrote marks come from history.csv."""
        parts = []
        hist = self._p("history")
        if hist.exists():
            h = pd.read_csv(hist)
            h["date"] = pd.to_datetime(h["date"])
            spot_col = "spot" if "spot" in h.columns else h.columns[1]   # (date, <underlying>, equity)
            parts.append(h[["date", "equity", spot_col]].rename(columns={spot_col: "spot"}).assign(source="reconstructed"))
        m = self.marks()
        realized = self._realized_by_day()
        if not m.empty:
            b = m[(m["row"] == "book")].copy()
            b["date"] = b["qt"].dt.normalize()
            last = b.sort_values("qt").groupby("date").tail(1)
            cum_real = (np.array([float(realized[realized.index <= d].sum()) for d in last["date"]])
                        if not realized.empty else 0.0)
            eq = last["unrealized"].to_numpy() + cum_real
            parts.append(pd.DataFrame({"date": last["date"].to_numpy(), "equity": eq, "spot": last["spot"].to_numpy(),
                                       "source": "bot"}))
        if not parts:
            return pd.DataFrame(columns=["date", "equity", "daily_pnl", "spot", "spot_ret", "source"])
        df = pd.concat(parts).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
        df["daily_pnl"] = df["equity"].diff().fillna(df["equity"])
        df["spot_ret"] = df["spot"].pct_change()
        return df

    def open_count_by_day(self, dates: pd.Series) -> pd.Series:
        """Spreads open at the END of each date, from the ledger (opened ≤ d < closed)."""
        t = self.trades()
        if t.empty:
            return pd.Series(0, index=dates)
        op = pd.to_datetime(t["opened"]).dt.normalize()
        cl = pd.to_datetime(t["closed"]).dt.normalize()
        return pd.Series([int(((op <= d) & (cl.isna() | (cl > d))).sum()) for d in dates], index=dates)

    def _realized_by_day(self) -> pd.Series:
        t = self.trades()
        if t.empty or t["realized"].isna().all():
            return pd.Series(dtype=float)
        c = t.dropna(subset=["realized"]).copy()
        c["date"] = pd.to_datetime(c["closed"]).dt.normalize()
        return c.groupby("date")["realized"].sum()
