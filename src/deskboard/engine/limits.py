"""Limit engine: rules over the book snapshot → `alert` events with a reason.

A rule is (name, scope, metric, bound). It is evaluated against every snapshot the engine
is asked to check; an alert is published when a rule *changes state* (OK → BREACH, or
BREACH → OK), never on every tick, so a channel like Telegram gets one message per event
and a "cleared" message when it resolves. Every alert carries the number, the bound, and
the position it applies to — a reason a trader can act on, not a colour.

Rules are plain data (YAML/JSON-able); the demo set lives in `DEMO_RULES`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from ..bus import Bus

Scope = Literal["book", "position"]
Op = Literal["max", "min"]

METRIC_LABEL = {
    "delta_usd": "net Δ$", "gamma_usd_1pct": "Γ$ per 1%", "vega_usd_1vol": "ν$ per vol pt",
    "theta_usd_day": "Θ$ per day", "pnl": "P&L today", "delta": "net Δ",
}


@dataclass(frozen=True)
class Rule:
    name: str
    scope: Scope        # "book" = totals; "position" = every position row
    metric: str         # a key of Book.totals() / position rows
    op: Op              # "max": breach when value > bound; "min": breach when value < bound
    bound: float

    def breached(self, value: float) -> bool:
        if value != value:
            return False
        return value > self.bound if self.op == "max" else value < self.bound

    def cleared(self, value: float, margin: float) -> bool:
        """Back inside by `margin` × |bound| — hysteresis so a value hovering at the limit
        does not flap BREACH/CLEARED on every tick."""
        if value != value:
            return False
        band = margin * abs(self.bound)
        return value <= self.bound - band if self.op == "max" else value >= self.bound + band


@dataclass
class Alert:
    ts: float
    rule: str
    scope: Scope
    target: str          # "book" or pos_id
    metric: str
    value: float
    bound: float
    state: Literal["BREACH", "CLEARED"]
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


DEMO_RULES = [
    Rule("book-delta", "book", "delta_usd", "max", 60_000),
    Rule("book-delta-short", "book", "delta_usd", "min", -60_000),
    Rule("book-vega", "book", "vega_usd_1vol", "min", -250),
    Rule("book-loss", "book", "pnl", "min", -1_500),
    Rule("position-loss", "position", "pnl", "min", -1_200),
    Rule("position-delta", "position", "delta_usd", "min", -90_000),
]


@dataclass
class LimitEngine:
    rules: list[Rule]
    bus: Bus | None = None
    clear_margin: float = 0.05
    state: dict[tuple[str, str], bool] = field(default_factory=dict)   # (rule, target) → breached?
    history: list[Alert] = field(default_factory=list)

    async def check(self, snapshot: dict, ts: float) -> list[Alert]:
        """Evaluate every rule; publish and return the alerts whose state changed."""
        fired: list[Alert] = []
        for rule in self.rules:
            targets = [("book", snapshot["totals"])] if rule.scope == "book" else [
                (r["pos_id"], r) for r in snapshot["positions"]]
            for target, row in targets:
                value = float(row.get(rule.metric, float("nan")))
                before = self.state.get((rule.name, target), False)
                now = rule.breached(value) if not before else not rule.cleared(value, self.clear_margin)
                if now == before:
                    continue
                self.state[(rule.name, target)] = now
                label = METRIC_LABEL.get(rule.metric, rule.metric)
                cmp = ">" if rule.op == "max" else "<"
                reason = (f"{label} {value:,.0f} {cmp} limit {rule.bound:,.0f}" if now
                          else f"{label} back to {value:,.0f}, inside limit {rule.bound:,.0f}")
                a = Alert(ts, rule.name, rule.scope, target, rule.metric, value, rule.bound,
                          "BREACH" if now else "CLEARED", reason)
                self.history.append(a)
                fired.append(a)
                if self.bus is not None:
                    await self.bus.publish("alert", ts, a.to_dict())
        return fired

    def open_breaches(self) -> list[tuple[str, str]]:
        return sorted(k for k, v in self.state.items() if v)
