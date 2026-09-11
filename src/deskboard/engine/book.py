"""The book: positions, marks, Greeks, and P&L attribution — a pure function of bus events.

Attribution runs leg by leg on every quote. Between two consecutive marks of a leg
(mid₀ at spot S₀, vol σ₀, time t₀ → mid₁ at S₁, σ₁, t₁), with Greeks taken at the old
mark:

    delta   = Δ₀ · (S₁ − S₀)
    gamma   = ½ Γ₀ · (S₁ − S₀)²
    vega    = ν₀ · (σ₁ − σ₀)
    theta   = Θ₀ · (t₁ − t₀)            [calendar days]
    residual = (mid₁ − mid₀) − (delta + gamma + vega + theta)

each scaled by signed quantity × multiplier. Fills contribute an execution component
(mid at fill − fill price) so that, for every position and for the book,

    realized_pnl == delta + gamma + vega + theta + execution + residual

holds exactly; the residual is reported, never hidden. Nothing here reads the wall clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..bus import Bus, Event
from . import greeks as bs

YEAR = 365.0
COMPONENTS = ("delta", "gamma", "vega", "theta", "execution", "residual")
EXPIRY_HOUR_UTC = 20  # 16:00 New York ≈ 20:00 UTC; good enough for T


def contract_key(leg: dict) -> str:
    if leg.get("sec_type", "OPT") == "STK":
        return leg["symbol"]
    return f"{leg['symbol']}:{leg['expiration']}:{float(leg['strike']):g}:{leg['right']}"


def signed_qty(leg: dict) -> float:
    q = float(leg["quantity"])
    return q if str(leg.get("side", "BUY")).upper() in ("BUY", "LONG") else -q


def multiplier(leg: dict) -> float:
    return 1.0 if leg.get("sec_type", "OPT") == "STK" else 100.0


def years_to_expiry(expiration: str, ts: float) -> float:
    exp = datetime.strptime(expiration, "%Y%m%d").replace(hour=EXPIRY_HOUR_UTC, tzinfo=timezone.utc)
    return max(0.0, (exp.timestamp() - ts) / 86400.0 / YEAR)


@dataclass
class Mark:
    ts: float
    bid: float
    ask: float
    mid: float
    spot: float
    iv: float = float("nan")
    g: bs.Greeks | None = None


@dataclass
class LegState:
    pos_id: str
    leg: dict
    key: str
    sq: float
    mult: float
    mark: Mark | None = None
    start_mid: float | None = None          # first mark after the leg exists in the book
    fill_px: float | None = None            # if the leg was filled during the session
    pnl: dict[str, float] = field(default_factory=lambda: {c: 0.0 for c in COMPONENTS})

    def realized(self) -> float:
        """Mark-to-market since the leg's first mark, plus what its fills earned or paid
        against the mid at fill time. Defined this way so it equals Σ components exactly;
        a fill on an already-marked leg is a price event on the existing quantity (v0.2
        does not model quantity changes)."""
        if self.mark is None or self.start_mid is None:
            return 0.0
        return (self.mark.mid - self.start_mid) * self.sq * self.mult + self.pnl["execution"]


class Book:
    """Subscribe to a Bus; expose `snapshot()` for any UI."""

    def __init__(self, r: float = 0.0, q: float = 0.0):
        self.r, self.q = r, q
        self.positions: dict[str, dict] = {}
        self.legs: dict[str, list[LegState]] = {}
        self.by_key: dict[str, list[LegState]] = {}
        self.spot: dict[str, float] = {}
        self.quotes: dict[str, dict] = {}      # last raw quote per contract, held or not
        self.last_ts: float | None = None
        self.n_events = 0

    # ---- wiring -------------------------------------------------------------------------
    def attach(self, bus: Bus) -> None:
        bus.subscribe("position", self.on_position)
        bus.subscribe("quote", self.on_quote)
        bus.subscribe("fill", self.on_fill)

    def load_positions(self, positions: list[dict]) -> None:
        for p in positions:
            self._add_position(p, fill_px_by_leg=None)

    # ---- handlers -----------------------------------------------------------------------
    def on_position(self, ev: Event) -> None:
        self.n_events += 1
        self.last_ts = ev.ts
        self._add_position(ev.payload, fill_px_by_leg=ev.payload.get("fills"))

    def on_fill(self, ev: Event) -> None:
        """A fill for an existing leg: execution P&L = (mid at fill − price) · signed qty · mult.
        The leg's mark-to-market base is unchanged, so the identity holds (see `realized`)."""
        self.n_events += 1
        self.last_ts = ev.ts
        p = ev.payload
        for ls in self.legs.get(p["pos_id"], []):
            if ls.key == contract_key(p) and ls.mark is not None:
                ls.fill_px = float(p["price"])
                ls.pnl["execution"] += (ls.mark.mid - ls.fill_px) * ls.sq * ls.mult

    def on_quote(self, ev: Event) -> None:
        self.n_events += 1
        self.last_ts = ev.ts
        p = ev.payload
        if p.get("sec_type", "OPT") == "STK":
            mid = (float(p["bid"]) + float(p["ask"])) / 2.0
            self.spot[p["symbol"]] = mid
            self.quotes[p["symbol"]] = {"ts": ev.ts, "bid": float(p["bid"]), "ask": float(p["ask"]), "mid": mid, "spot": mid, **p}
            self._mark_stock_legs(p["symbol"], ev.ts, float(p["bid"]), float(p["ask"]))
            return
        key = contract_key(p)
        S = self.spot.get(p["symbol"])
        if S is None:
            return
        bid, ask = float(p["bid"]), float(p["ask"])
        mid = (bid + ask) / 2.0
        self.quotes[key] = {"ts": ev.ts, "bid": bid, "ask": ask, "mid": mid, "spot": S, **p}
        legs = self.by_key.get(key)
        if not legs:
            return
        T = years_to_expiry(p["expiration"], ev.ts)
        iv = bs.implied_vol(mid, S, float(p["strike"]), T, p["right"], self.r, self.q)
        g = bs.greeks(S, float(p["strike"]), T, iv, p["right"], self.r, self.q) if iv == iv else None
        new = Mark(ev.ts, bid, ask, mid, S, iv, g)
        for ls in legs:
            self._attribute(ls, new)
            ls.mark = new
            if ls.start_mid is None:
                ls.start_mid = mid

    # ---- internals ----------------------------------------------------------------------
    def _add_position(self, pos: dict, fill_px_by_leg: list[float] | None) -> None:
        pid = pos["pos_id"]
        self.positions[pid] = pos
        self.legs[pid] = []
        for i, leg in enumerate(pos["legs"]):
            ls = LegState(pid, leg, contract_key(leg), signed_qty(leg), multiplier(leg))
            if fill_px_by_leg is not None:
                ls.fill_px = float(fill_px_by_leg[i])
            self.legs[pid].append(ls)
            self.by_key.setdefault(ls.key, []).append(ls)
        hedge = float(pos.get("hedge_shares", 0) or 0)
        if hedge:
            leg = {"symbol": pos["legs"][0]["symbol"], "sec_type": "STK", "side": "BUY" if hedge > 0 else "SELL",
                   "quantity": abs(hedge)}
            ls = LegState(pid, leg, contract_key(leg), hedge, 1.0)
            self.legs[pid].append(ls)
            self.by_key.setdefault(ls.key, []).append(ls)
        # a position that arrives mid-session with fills is marked at the last quote before the
        # fill; execution P&L is the fill's distance from that mid, attribution starts there
        for ls in self.legs[pid]:
            if ls.fill_px is not None and ls.key in self.quotes:
                m = self._mark_from_quote(self.quotes[ls.key])
                ls.mark = m
                ls.start_mid = m.mid
                ls.pnl["execution"] += (m.mid - ls.fill_px) * ls.sq * ls.mult

    def _mark_from_quote(self, q: dict) -> Mark:
        if q.get("sec_type", "OPT") == "STK":
            return Mark(q["ts"], q["bid"], q["ask"], q["mid"], q["mid"], 0.0, bs.Greeks(q["mid"], 1.0, 0.0, 0.0, 0.0, 0.0))
        T = years_to_expiry(q["expiration"], q["ts"])
        iv = bs.implied_vol(q["mid"], q["spot"], float(q["strike"]), T, q["right"], self.r, self.q)
        g = bs.greeks(q["spot"], float(q["strike"]), T, iv, q["right"], self.r, self.q) if iv == iv else None
        return Mark(q["ts"], q["bid"], q["ask"], q["mid"], q["spot"], iv, g)

    def _mark_stock_legs(self, symbol: str, ts: float, bid: float, ask: float) -> None:
        mid = (bid + ask) / 2.0
        for ls in self.by_key.get(symbol, []):
            new = Mark(ts, bid, ask, mid, mid, 0.0, bs.Greeks(mid, 1.0, 0.0, 0.0, 0.0, 0.0))
            if ls.mark is not None:
                ls.pnl["delta"] += (mid - ls.mark.mid) * ls.sq * ls.mult
            ls.mark = new
            if ls.start_mid is None:
                ls.start_mid = mid

    def _attribute(self, ls: LegState, new: Mark) -> None:
        old = ls.mark
        if old is None:
            return
        scale = ls.sq * ls.mult
        if old.g is None:  # no Greeks at the old mark (IV outside no-arb bounds): the whole move is residual
            ls.pnl["residual"] += (new.mid - old.mid) * scale
            return
        dS = new.spot - old.spot
        dsig = (new.iv - old.iv) if (new.iv == new.iv and old.iv == old.iv) else 0.0
        ddays = (new.ts - old.ts) / 86400.0
        delta = old.g.delta * dS
        gamma = 0.5 * old.g.gamma * dS * dS
        vega = old.g.vega * dsig
        theta = old.g.theta * ddays
        dP = new.mid - old.mid
        ls.pnl["delta"] += delta * scale
        ls.pnl["gamma"] += gamma * scale
        ls.pnl["vega"] += vega * scale
        ls.pnl["theta"] += theta * scale
        ls.pnl["residual"] += (dP - delta - gamma - vega - theta) * scale

    # ---- views --------------------------------------------------------------------------
    def leg_rows(self) -> list[dict]:
        rows = []
        for pid, legs in self.legs.items():
            for ls in legs:
                m, g = ls.mark, (ls.mark.g if ls.mark else None)
                S = m.spot if m else float("nan")
                scale = ls.sq * ls.mult
                rows.append({
                    "pos_id": pid, "sleeve": self.positions[pid].get("sleeve", ""), "contract": ls.key,
                    "qty": ls.sq, "mid": m.mid if m else float("nan"), "iv": m.iv if m else float("nan"),
                    "delta": g.delta * scale if g else float("nan"),
                    "delta_usd": g.delta * S * scale if g else float("nan"),
                    "gamma_usd_1pct": 0.5 * g.gamma * (0.01 * S) ** 2 * scale if g else float("nan"),
                    "vega_usd_1vol": g.vega * 0.01 * scale if g else float("nan"),
                    "theta_usd_day": g.theta * scale if g else float("nan"),
                    "pnl": ls.realized(), **{f"pnl_{c}": ls.pnl[c] for c in COMPONENTS},
                })
        return rows

    def position_rows(self, legs: list[dict] | None = None) -> list[dict]:
        out: dict[str, dict] = {}
        for r in (legs if legs is not None else self.leg_rows()):
            o = out.setdefault(r["pos_id"], {"pos_id": r["pos_id"], "sleeve": r["sleeve"], "legs": 0,
                                             **{k: 0.0 for k in ("delta", "delta_usd", "gamma_usd_1pct", "vega_usd_1vol",
                                                                  "theta_usd_day", "pnl")},
                                             **{f"pnl_{c}": 0.0 for c in COMPONENTS}})
            o["legs"] += 1
            for k in list(o):
                if k not in ("pos_id", "sleeve", "legs") and r[k] == r[k]:
                    o[k] += r[k]
        return list(out.values())

    def totals(self, positions: list[dict] | None = None) -> dict:
        rows = positions if positions is not None else self.position_rows()
        keys = ["delta", "delta_usd", "gamma_usd_1pct", "vega_usd_1vol", "theta_usd_day", "pnl"] + [f"pnl_{c}" for c in COMPONENTS]
        t = {k: float(sum(r[k] for r in rows)) for k in keys}
        t["identity_gap"] = t["pnl"] - sum(t[f"pnl_{c}"] for c in COMPONENTS)
        t["n_positions"] = len(rows)
        t["n_events"] = self.n_events
        t["last_ts"] = self.last_ts
        t["spot"] = dict(self.spot)
        return t

    def snapshot(self, include_legs: bool = True) -> dict:
        legs = self.leg_rows()
        positions = self.position_rows(legs)
        return {"totals": self.totals(positions), "positions": positions, "legs": legs if include_legs else []}

    def state_hash(self, decimals: int = 6) -> str:
        """SHA-256 of the snapshot with floats rounded to `decimals`.

        Bit-identical within a platform; across platforms libm differences move the last
        bits of norm.cdf / Brent, so the cross-platform guarantee is "identical to 1e-6",
        which is what CI checks against the committed hash."""
        import hashlib
        import json

        def rnd(x):
            if isinstance(x, float):
                return 0.0 if x != x else round(x, decimals) + 0.0  # NaN → 0.0, −0.0 → 0.0
            if isinstance(x, dict):
                return {k: rnd(v) for k, v in x.items()}
            if isinstance(x, list):
                return [rnd(v) for v in x]
            return x

        blob = json.dumps(rnd(self.snapshot()), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()
