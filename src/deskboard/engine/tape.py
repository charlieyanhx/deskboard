"""The event tape: what is coming in, right now.

A bus subscriber that keeps the last N events of every topic in a ring buffer plus a
per-contract "last quote" table with its age, and per-minute counts by topic. Pure
bookkeeping — nothing here feeds back into the book — and it works on replay and live
alike, so a stalled feed looks the same in both: ages grow, counts stop.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field

from ..bus import TOPICS, Bus, Event
from .book import contract_key


@dataclass
class Tape:
    maxlen: int = 2000
    events: deque = field(default_factory=lambda: deque(maxlen=2000))
    rare: deque = field(default_factory=lambda: deque(maxlen=500))  # fills/positions/alerts survive the quote flood
    last_quote: dict[str, dict] = field(default_factory=dict)      # contract → {ts, bid, ask, wall}
    per_minute: Counter = field(default_factory=Counter)           # (minute_bucket, topic) → n
    counts: Counter = field(default_factory=Counter)               # topic → n
    first_wall: float | None = None

    def attach(self, bus: Bus) -> "Tape":
        self.events = deque(maxlen=self.maxlen)
        for t in TOPICS:
            bus.subscribe(t, self._on)
        return self

    async def _on(self, ev: Event) -> None:
        wall = time.time()
        if self.first_wall is None:
            self.first_wall = wall
        self.counts[ev.topic] += 1
        self.per_minute[(int(ev.ts // 60), ev.topic)] += 1
        row = {"seq": ev.seq, "ts": ev.ts, "wall": wall, "topic": ev.topic, **_summ(ev)}
        self.events.append(row)
        if ev.topic != "quote":
            self.rare.append(row)
        if ev.topic == "quote":
            p = ev.payload
            key = p["symbol"] if p.get("sec_type", "STK") == "STK" else contract_key(p)
            self.last_quote[key] = {"ts": ev.ts, "wall": wall, "bid": p.get("bid"), "ask": p.get("ask"),
                                    "sec_type": p.get("sec_type", "STK")}

    def recent(self, n: int = 200, topic: str | None = None) -> list[dict]:
        """Newest first. Non-quote topics come from the ``rare`` buffer, which a quote flood cannot evict."""
        src = self.events if topic in (None, "quote") else self.rare
        rows = [r for r in reversed(src) if topic is None or r["topic"] == topic]
        return rows[:n]

    def quote_ages(self, now: float | None = None) -> list[dict]:
        """Per contract: bid, ask, spread, seconds since its last quote (event clock)."""
        now = now if now is not None else (max((q["ts"] for q in self.last_quote.values()), default=0.0))
        out = []
        for key, q in self.last_quote.items():
            b, a = q.get("bid"), q.get("ask")
            out.append({"contract": key, "bid": b, "ask": a, "spread": (a - b) if (a is not None and b is not None) else None,
                        "age_s": now - q["ts"], "sec_type": q["sec_type"]})
        return sorted(out, key=lambda r: (r["sec_type"] != "STK", r["contract"]))

    def rate(self, window_min: int = 5, now_ts: float | None = None) -> dict[str, float]:
        """Events per minute by topic over the last ``window_min`` event-clock minutes."""
        if not self.events:
            return {}
        now_ts = now_ts if now_ts is not None else self.events[-1]["ts"]
        lo = int(now_ts // 60) - window_min + 1
        agg: Counter = Counter()
        for (m, t), n in self.per_minute.items():
            if m >= lo:
                agg[t] += n
        return {t: agg[t] / window_min for t in TOPICS if agg[t]}


def _summ(ev: Event) -> dict:
    p = ev.payload
    if ev.topic == "quote":
        key = p["symbol"] if p.get("sec_type", "STK") == "STK" else contract_key(p)
        return {"key": key, "detail": f"{p.get('bid')} / {p.get('ask')}"}
    if ev.topic == "fill":
        return {"key": contract_key(p) if p.get("sec_type", "OPT") == "OPT" else p.get("symbol", ""),
                "detail": f"{p.get('side')} {p.get('qty')} @ {p.get('price')} ({p.get('pos_id', '')})"}
    if ev.topic == "position":
        return {"key": p.get("pos_id", ""), "detail": f"{p.get('sleeve', '')} {len(p.get('legs', []))} legs"}
    if ev.topic == "alert":
        return {"key": p.get("rule", ""), "detail": f"{p.get('state', '')} {p.get('target', '')}: {p.get('reason', '')}"}
    if ev.topic == "clock":
        return {"key": "clock", "detail": ", ".join(f"{k}={v}" for k, v in p.items())}
    return {"key": "", "detail": str(p)[:80]}
