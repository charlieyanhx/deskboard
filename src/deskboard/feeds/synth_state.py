"""A synthetic strategy STATE DIRECTORY (feeds/statefiles.py contract) for the demo and the tests.

Thirty business days of a fictional short-premium book on ``SYN``: two credit spreads a day
when the signal fires, held six days, marked daily at Black-Scholes mids on a drifting vol
surface, with a ledger whose per-leg decision quotes and fills are consistent with those
marks. Also writes the reference the pages compare against (expected pace, daily σ, the
per-spread P&L-vs-return bands) and a two-year regime history. Seeded; ``deskboard record``
regenerates every file byte for byte.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..engine import greeks as bs

SYMBOL = "SYN"
HOLD_DAYS = 6
SNAPS = ["10:30:00", "11:30:00", "12:30:00", "13:30:00", "14:30:00", "15:30:00", "16:15:00"]


def _iv(S: float, K: float, T: float, base: float) -> float:
    k = np.log(K / S)
    return float(base - 0.35 * k + 0.9 * k * k + 0.002 / max(T, 0.01) ** 0.5)


def _mid(S, K, T, base, right):
    return bs.price(S, K, T, _iv(S, K, T, base), right)


def demo_state(out_dir: str | Path, seed: int = 7, session_date: str = "2026-06-15", n_days: int = 30) -> dict:
    """Write the state directory under ``out_dir``; return a small summary for the caller."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed + 11)
    end = pd.Timestamp(session_date)
    days = pd.bdate_range(end=end, periods=n_days)
    spot = 500.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.011, n_days)))
    base_vol = 0.17 + 0.03 * np.sin(np.linspace(0, 3, n_days)) + rng.normal(0, 0.004, n_days).cumsum() * 0.2

    positions: dict[str, dict] = {}        # ticket -> dict(open_day_i, legs, credit, expiry)
    ledger: list[dict] = []
    marks: list[dict] = []
    funnel: list[dict] = []
    regime: list[dict] = []
    tid = 0

    def quotes(i, K, right, T):
        m = _mid(spot[i], K, T, base_vol[i], right)
        half = max(0.02, 0.004 * m)
        return round(m - half, 2), round(m + half, 2)

    for i, d in enumerate(days):
        ds = d.strftime("%Y-%m-%d")
        exp_dt = d + timedelta(days=38)
        exp_c = exp_dt.strftime("%Y%m%d")
        exp_d = exp_dt.strftime("%Y-%m-%d")
        T = 38 / 365
        # ---- closes: tickets that reached HOLD_DAYS
        for t, p in list(positions.items()):
            if i - p["i"] >= HOLD_DAYS:
                Tx = (pd.Timestamp(p["expiry"]) - d).days / 365
                legs = []
                net = 0.0
                for lg in p["legs"]:
                    b, a = quotes(i, lg["strike"], lg["right"], Tx)
                    side = "BUY" if lg["side"] == "SELL" else "SELL"       # reverse
                    fill = a if side == "BUY" else b
                    net += fill if side == "BUY" else -fill
                    legs.append(dict(side=side, sec_type="OPT", qty=1, bid=b, ask=a, fill=fill, right=lg["right"],
                                     strike=lg["strike"], expiry=p["expiry"]))
                ledger.append(dict(schema=1, event="fill", ts=f"{ds}T13:30:00", date=ds, sleeve=p["sleeve"], action="close",
                                   ticket_id=t, intended_limit=round(net + 0.02, 3), fill_net=round(net, 3), commission=1.3,
                                   note=f"fresh=2/2 basis=cross rung=1 latency_ms={int(rng.integers(900, 6000))}", legs=legs))
                del positions[t]
        # ---- signal + entries at 10:30
        cands = int(rng.integers(0, 16))
        fires = cands >= 6 and len(positions) < 8
        n_new = 2 if fires else 0
        fills_ok = 0
        for k in range(n_new):
            tid += 1
            t = f"t{tid:04d}"
            short = float(round(spot[i] * (0.965 - 0.005 * k)))
            wing = short - 30.0
            sleeve = "A"
            legs = []
            net = 0.0
            for K, side in ((short, "SELL"), (wing, "BUY")):
                b, a = quotes(i, K, "P", T)
                fill = b if side == "SELL" else a
                net += fill if side == "BUY" else -fill
                legs.append(dict(side=side, sec_type="OPT", qty=1, bid=b, ask=a, fill=fill, right="P", strike=K, expiry=exp_d))
            if rng.random() < 0.92:                                        # a miss now and then
                fills_ok += 1
                ledger.append(dict(schema=1, event="fill", ts=f"{ds}T10:31:00", date=ds, sleeve=sleeve, action="open",
                                   ticket_id=t, intended_limit=round(net + 0.02, 3), fill_net=round(net, 3), commission=1.3,
                                   note=f"fresh=2/2 basis=cross rung=1 latency_ms={int(rng.integers(900, 6000))}", legs=legs))
                positions[t] = dict(i=i, sleeve=sleeve, expiry=exp_d, credit=round(-net * 100, 2),
                                    legs=[dict(symbol=SYMBOL, sec_type="OPT", expiration=exp_c, strike=lg["strike"], right="P",
                                               side=lg["side"], quantity=1.0, avg_fill_price=lg["fill"]) for lg in legs])
        if n_new:
            funnel.append(dict(qt=f"{ds} 10:30:00", sleeve="A", candidates=cands, after_dedupe=max(cands - 1, 0), tickets=n_new,
                               attempted=n_new, filled=fills_ok, cap_remaining=2 - fills_ok, blocked=None))
        elif cands:
            funnel.append(dict(qt=f"{ds} 10:30:00", sleeve="A", candidates=cands, after_dedupe=cands, tickets=0, attempted=0,
                               filled=0, cap_remaining=2, blocked="below_threshold" if cands < 6 else "position_cap"))
        # ---- marks at every snap (mid), EOD row flagged
        for snap in SNAPS:
            qt = f"{ds} {snap}"
            book = 0.0
            for t, p in positions.items():
                Tx = (pd.Timestamp(p["expiry"]) - d).days / 365
                net = 0.0
                for lg in p["legs"]:
                    m = _mid(spot[i], lg["strike"], Tx, base_vol[i] + (0.001 if snap != "16:15:00" else 0.0), lg["right"])
                    net += (m if lg["side"] == "BUY" else -m) * 100
                unreal = round(p["credit"] + net, 2)
                book += unreal
                marks.append(dict(qt=qt, kind="eod" if snap == "16:15:00" else "snap", row="position", pos_id=t, sleeve=p["sleeve"],
                                  entry_qt=f"{days[p['i']]:%Y-%m-%d} 10:30:00", snaps_held=(i - p["i"]) * len(SNAPS),
                                  entry_net_credit=p["credit"], mark_net=round(-net, 2), unrealized=unreal, marked=True,
                                  spot=round(float(spot[i]), 2)))
            marks.append(dict(qt=qt, kind="eod" if snap == "16:15:00" else "snap", row="book", n_open=len(positions), n_unmarked=0,
                              unrealized=round(book, 2), realized_today=0.0, spot=round(float(spot[i]), 2)))
            if snap == "10:30:00":
                atm = _iv(spot[i], spot[i], 30 / 365, base_vol[i])
                regime.append(dict(qt=qt, spot=round(float(spot[i]), 2), universe=int(300 + rng.integers(-40, 40)),
                                   atm_iv=round(atm, 4), atm_dte=30,
                                   term_slope_front=round(float(-0.012 + rng.normal(0, 0.004)), 4),
                                   term_slope_back=round(float(0.008 + rng.normal(0, 0.002)), 4),
                                   skew_25=round(float(0.045 + rng.normal(0, 0.006)), 4),
                                   gex_bn_per_pct=round(float(rng.normal(1.0, 0.8)), 3),
                                   dex_m_shares=round(float(rng.normal(-20, 15)), 2), a_candidates=cands))

    # ---- surface for the last day (csv: deterministic text, diffable in CI)
    i = n_days - 1
    rows = []
    for dte in (24, 31, 38):
        exp_s = (days[i] + timedelta(days=dte)).strftime("%Y-%m-%d")
        for K in np.arange(round(spot[i] * 0.85 / 5) * 5, round(spot[i] * 1.12 / 5) * 5 + 1, 5.0):
            for right in ("P", "C"):
                T = dte / 365
                iv = _iv(spot[i], K, T, base_vol[i])
                m = bs.price(spot[i], K, T, iv, right)
                g = bs.greeks(spot[i], K, T, iv, right)
                half = max(0.02, 0.004 * m)
                rows.append(dict(strike=float(K), expiration=exp_s, option_type=right, implied_volatility=round(iv, 5),
                                 delta=round(g.delta, 5), gamma=round(g.gamma, 6), theta=round(g.theta, 5),
                                 vega=round(g.vega, 5), bid=round(m - half, 2), ask=round(m + half, 2),
                                 open_interest=int(200 * np.exp(-((K - spot[i]) / spot[i] / 0.05) ** 2) + 5),
                                 trade_volume=0, spot=round(float(spot[i]), 2), qt=f"{days[i]:%Y-%m-%d} 10:30:00"))
    sdir = out / "surfaces" / days[i].strftime("%Y-%m-%d")
    sdir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(sdir / "1030.csv", index=False)

    # ---- files
    pos_out = [dict(pos_id=t, sleeve=p["sleeve"], entry_qt=f"{days[p['i']]:%Y-%m-%d} 10:30:00", lot_size=1,
                    entry_net_credit=p["credit"], snaps_held=(n_days - 1 - p["i"]) * len(SNAPS), hedge_shares=0, legs=p["legs"])
               for t, p in positions.items()]
    (out / "positions.json").write_text(json.dumps({"positions": pos_out}, indent=1))
    (out / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ledger))
    (out / "marks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in marks))
    (out / "funnel.jsonl").write_text("".join(json.dumps(r) + "\n" for r in funnel))
    (out / "regime.jsonl").write_text("".join(json.dumps(r) + "\n" for r in regime))

    # ---- reference: pace / σ from a longer simulated history of the same rules, plus the
    # per-spread P&L-vs-return bands by moneyness (what a backtest tape would supply)
    ref = dict(
        source="synthetic (feeds/synth_state.demo_state); a real desk supplies its backtest's numbers here",
        pace_usd_per_bday=40.0, net_pace_usd_per_bday=36.0, daily_sigma_usd=520.0, tape_median_spot=470.0,
        ks_edges=[0.90, 0.96, 0.975, 0.985, 0.995, 1.01],
        # what the backtest earns in each condition — the Model page pairs the live book with these
        conditional=[
            dict(dim="term structure", bucket="backwardation (slope>0 front-30)", unit="$ per open spread per day",
                 n=1800, modeled_mean=-0.2, modeled_median=6.0),
            dict(dim="term structure", bucket="contango", unit="$ per open spread per day",
                 n=2600, modeled_mean=34.5, modeled_median=29.8),
            dict(dim="ATM IV", bucket="low IV (<13)", unit="$ per open spread per day", n=900, modeled_mean=34.7, modeled_median=29.8),
            dict(dim="ATM IV", bucket="mid IV (13-18)", unit="$ per open spread per day", n=1500, modeled_mean=29.9, modeled_median=29.5),
            dict(dim="ATM IV", bucket="high IV (>18)", unit="$ per open spread per day", n=2100, modeled_mean=6.6, modeled_median=8.5),
            dict(dim="SPY day", bucket="<-0.75%", unit="$ per open spread per day", n=825, modeled_mean=-196.3, modeled_median=-150.5),
            dict(dim="SPY day", bucket="-0.75..-0.25%", unit="$ per open spread per day", n=710, modeled_mean=-47.9, modeled_median=-43.5),
            dict(dim="SPY day", bucket="flat ±0.25%", unit="$ per open spread per day", n=976, modeled_mean=12.0, modeled_median=12.5),
            dict(dim="SPY day", bucket="+0.25..+0.75%", unit="$ per open spread per day", n=800, modeled_mean=68.6, modeled_median=61.3),
            dict(dim="SPY day", bucket=">+0.75%", unit="$ per open spread per day", n=1183, modeled_mean=185.6, modeled_median=157.0),
            dict(dim="term structure (at entry)", bucket="backwardation (slope>0 front-30)", unit="$ per trade realized",
                 n=376, modeled_mean=193.5, modeled_median=212.4),
            dict(dim="term structure (at entry)", bucket="contango", unit="$ per trade realized",
                 n=477, modeled_mean=78.8, modeled_median=110.4),
        ],
        modeled_friction=dict(entry_half_spread_usd=5.0, exit_half_spread_usd=4.0,
                              round_trip_spread_usd=9.0, commission_rt_usd=2.6, total_friction_usd=11.6,
                              source="synthetic: what the backtest charges per fill"),
        by_ks=[dict(ks_lo=0.90, ks_hi=0.96, n=400, slope_per_pct_per100spot=0.0130, intercept_per100spot=0.0060, deq_p10=-0.01, deq_p50=0.13, deq_p90=0.21),
               dict(ks_lo=0.96, ks_hi=0.975, n=500, slope_per_pct_per100spot=0.0170, intercept_per100spot=0.0060, deq_p10=0.07, deq_p50=0.17, deq_p90=0.23),
               dict(ks_lo=0.975, ks_hi=0.985, n=500, slope_per_pct_per100spot=0.0210, intercept_per100spot=0.0050, deq_p10=0.10, deq_p50=0.21, deq_p90=0.28),
               dict(ks_lo=0.985, ks_hi=0.995, n=450, slope_per_pct_per100spot=0.0250, intercept_per100spot=0.0040, deq_p10=0.11, deq_p50=0.25, deq_p90=0.33),
               dict(ks_lo=0.995, ks_hi=1.01, n=350, slope_per_pct_per100spot=0.0330, intercept_per100spot=0.0030, deq_p10=0.16, deq_p50=0.33, deq_p90=0.38)],
    )
    (out / "reference.json").write_text(json.dumps(ref, indent=1))
    hrng = np.random.default_rng(seed + 23)
    hdays = pd.bdate_range(end=days[0] - pd.tseries.offsets.BDay(1), periods=500)
    hist = pd.DataFrame(dict(
        qt=[f"{x:%Y-%m-%d} 10:30:00" for x in hdays],
        atm_iv=(0.16 + 0.05 * np.abs(hrng.standard_t(4, 500)) * 0.4).round(4),
        term_slope_front=hrng.normal(-0.01, 0.012, 500).round(4), term_slope_back=hrng.normal(0.008, 0.004, 500).round(4),
        skew_25=hrng.normal(0.045, 0.012, 500).round(4), gex_bn_per_pct=hrng.normal(0.8, 1.1, 500).round(3),
        dex_m_shares=hrng.normal(-15, 25, 500).round(2)))
    hist.to_csv(out / "regime_history.csv", index=False)
    return dict(days=n_days, tickets=tid, ledger_rows=len(ledger), marks_rows=len(marks), open=len(pos_out))


def load_reference(state_dir: str | Path) -> tuple[dict, pd.DataFrame | None]:
    """``reference.json`` and ``regime_history.(csv|parquet)`` from a state dir, if present."""
    d = Path(state_dir)
    ref = json.loads((d / "reference.json").read_text()) if (d / "reference.json").exists() else {}
    hist = None
    for name in ("regime_history.parquet", "regime_history.csv"):
        p = d / name
        if p.exists():
            hist = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
            hist["qt"] = pd.to_datetime(hist["qt"])
            break
    return ref, hist


if __name__ == "__main__":
    print(demo_state("data/demo/state"))
    print(datetime.now())
