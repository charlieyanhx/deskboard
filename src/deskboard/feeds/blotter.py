"""Positions from a file. Two shapes are accepted, both a list of positions with legs:

* the demo blotter (`data/demo/blotter.json`) — `[{pos_id, sleeve, legs: [...], hedge_shares?}]`
* a live bot state file — `{"positions": [ ... same shape ... ], ...}`

Legs carry `symbol, sec_type, expiration (YYYYMMDD), strike, right, side, quantity` and an
optional `avg_fill_price`. The dashboard never writes to either file and never submits orders.
"""

from __future__ import annotations

import json
from pathlib import Path

REQUIRED_LEG = {"symbol", "expiration", "strike", "right", "side", "quantity"}


def load_blotter(path: str | Path) -> list[dict]:
    raw = json.loads(Path(path).read_text())
    positions = raw["positions"] if isinstance(raw, dict) else raw
    out = []
    for p in positions:
        legs = []
        for leg in p["legs"]:
            if leg.get("sec_type", "OPT") == "OPT" and not REQUIRED_LEG <= set(leg):
                raise ValueError(f"{p.get('pos_id')}: leg missing {sorted(REQUIRED_LEG - set(leg))}")
            legs.append({**leg, "strike": float(leg["strike"]), "quantity": float(leg["quantity"])})
        out.append({"pos_id": str(p["pos_id"]), "sleeve": p.get("sleeve", ""), "legs": legs,
                    "hedge_shares": float(p.get("hedge_shares", 0) or 0)})
    return out
