"""Record a synthetic trading session: one day of 1-minute quotes for a demo blotter.

Underlying: GBM at 1-minute steps from 09:30 to 16:00 New York. Options: mid from
Black-Scholes on a skewed, slowly drifting vol surface (so vega and theta P&L are real),
bid/ask around it; a quote outage or two; one position opened mid-session with fills.
Seeded, so `deskboard record` regenerates the same file byte for byte.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..engine import greeks as bs
from ..engine.book import contract_key

SYMBOL = "SYN"
MINUTES = 390


def demo_blotter(session_date: str = "2026-06-15") -> list[dict]:
    d = datetime.strptime(session_date, "%Y-%m-%d")
    e45 = (d + timedelta(days=45)).strftime("%Y%m%d")
    e30 = (d + timedelta(days=30)).strftime("%Y%m%d")
    e10 = (d + timedelta(days=10)).strftime("%Y%m%d")

    def leg(strike, right, side, qty, exp):
        return {"symbol": SYMBOL, "sec_type": "OPT", "expiration": exp, "strike": float(strike),
                "right": right, "side": side, "quantity": float(qty), "avg_fill_price": None}

    return [
        {"pos_id": "A-0601", "sleeve": "A", "legs": [leg(480, "P", "SELL", 4, e45), leg(440, "P", "BUY", 4, e45)]},
        {"pos_id": "C-0603", "sleeve": "C", "legs": [leg(470, "P", "SELL", 2, e30), leg(530, "C", "SELL", 2, e30)],
         "hedge_shares": -40},
        {"pos_id": "S-0610", "sleeve": "S", "legs": [leg(490, "P", "BUY", 3, e10), leg(510, "C", "SELL", 3, e10)]},
    ]


def intraday_position(session_date: str = "2026-06-15") -> dict:
    d = datetime.strptime(session_date, "%Y-%m-%d")
    e45 = (d + timedelta(days=45)).strftime("%Y%m%d")
    return {"pos_id": "A-0615", "sleeve": "A", "legs": [
        {"symbol": SYMBOL, "sec_type": "OPT", "expiration": e45, "strike": 475.0, "right": "P", "side": "SELL", "quantity": 3.0},
        {"symbol": SYMBOL, "sec_type": "OPT", "expiration": e45, "strike": 435.0, "right": "P", "side": "BUY", "quantity": 3.0},
    ]}


def _iv(S: float, K: float, T: float, base: float) -> float:
    k = np.log(K / S)
    return base - 0.35 * k + 0.9 * k * k + 0.02 / max(T, 0.01) ** 0.5 * 0.1


def record(seed: int = 7, session_date: str = "2026-06-15", spot0: float = 500.0, sigma_daily: float = 0.012) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = datetime.strptime(session_date, "%Y-%m-%d").replace(hour=13, minute=30, tzinfo=timezone.utc)  # 09:30 NY (EDT)
    t0 = start.timestamp()
    rets = rng.normal(0, sigma_daily / np.sqrt(MINUTES), MINUTES)
    rets[200:206] -= 0.0015  # a small air-pocket after lunch
    spot = spot0 * np.exp(np.cumsum(rets))
    base_vol = 0.17 + 0.02 * np.sin(np.linspace(0, 2.5, MINUTES)) + rng.normal(0, 0.0015, MINUTES).cumsum() * 0.3
    base_vol[200:] += 0.012

    blotter = demo_blotter(session_date)
    intraday = intraday_position(session_date)
    contracts: dict[str, dict] = {}
    for pos in blotter + [intraday]:
        for leg in pos["legs"]:
            contracts[contract_key(leg)] = leg
    outage = {contract_key(blotter[1]["legs"][1]): set(range(150, 158))}

    rows: list[dict] = []
    for i in range(MINUTES):
        ts = t0 + 60.0 * i
        S = float(spot[i])
        half_s = 0.01
        rows.append({"ts": ts, "topic": "quote", "payload": json.dumps(
            {"symbol": SYMBOL, "sec_type": "STK", "bid": round(S - half_s, 2), "ask": round(S + half_s, 2)})})
        for key, leg in contracts.items():
            if i in outage.get(key, ()):
                continue
            exp = datetime.strptime(leg["expiration"], "%Y%m%d").replace(hour=20, tzinfo=timezone.utc).timestamp()
            T = (exp - ts) / 86400.0 / 365.0
            iv = float(_iv(S, leg["strike"], T, float(base_vol[i])))
            mid = bs.price(S, leg["strike"], T, iv, leg["right"])
            half = max(0.02, 0.004 * mid + 0.01 * abs(np.log(leg["strike"] / S)) * 5)
            rows.append({"ts": ts + 0.2, "topic": "quote", "payload": json.dumps(
                {"symbol": SYMBOL, "sec_type": "OPT", "expiration": leg["expiration"], "strike": leg["strike"],
                 "right": leg["right"], "bid": round(mid - half, 2), "ask": round(mid + half, 2)})})
        if i == 90:  # 11:00 — open A-0615 at the ask/bid (cross the spread)
            fills = []
            for leg in intraday["legs"]:
                q = next(r for r in reversed(rows) if r["topic"] == "quote" and json.loads(r["payload"]).get("strike") == leg["strike"]
                         and json.loads(r["payload"]).get("right") == leg["right"])
                p = json.loads(q["payload"])
                fills.append(p["bid"] if leg["side"] == "SELL" else p["ask"])
            rows.append({"ts": ts + 0.5, "topic": "position", "payload": json.dumps({**intraday, "fills": fills})})
    return pd.DataFrame(rows)
