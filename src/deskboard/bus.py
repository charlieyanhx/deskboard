"""asyncio event bus. Topics: quote, trade, fill, position, alert, clock.

Deterministic by construction: events are delivered to subscribers in the order they are
published, on the publishing task, synchronously. Nothing downstream depends on wall-clock
time — replaying the same file yields the same sequence of handler calls, which is what
the determinism test checks. Async subscribers are awaited in order.
"""

from __future__ import annotations

import inspect
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

TOPICS = ("quote", "trade", "fill", "position", "alert", "clock")


@dataclass(frozen=True)
class Event:
    topic: str
    ts: float          # event time, seconds since epoch (from the feed, never wall clock)
    payload: dict[str, Any]
    seq: int = 0       # assigned by the bus, monotone


Handler = Callable[[Event], None | Awaitable[None]]


@dataclass
class Bus:
    _subs: dict[str, list[Handler]] = field(default_factory=lambda: defaultdict(list))
    seq: int = 0
    published: int = 0
    dispatch_ns: list[int] = field(default_factory=list)   # per-event handler latency, for the budget test

    def subscribe(self, topic: str, handler: Handler) -> None:
        if topic not in TOPICS:
            raise ValueError(f"unknown topic {topic!r}; known: {TOPICS}")
        self._subs[topic].append(handler)

    async def publish(self, topic: str, ts: float, payload: dict[str, Any]) -> Event:
        if topic not in TOPICS:
            raise ValueError(f"unknown topic {topic!r}")
        self.seq += 1
        ev = Event(topic, ts, payload, self.seq)
        t0 = time.perf_counter_ns()
        for h in list(self._subs[topic]):
            r = h(ev)
            if inspect.isawaitable(r):
                await r
        self.dispatch_ns.append(time.perf_counter_ns() - t0)
        self.published += 1
        return ev

    def latency_ms(self) -> dict[str, float]:
        if not self.dispatch_ns:
            return {"p50": float("nan"), "p99": float("nan"), "n": 0}
        xs = sorted(self.dispatch_ns)
        q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))] / 1e6  # noqa: E731
        return {"p50": q(0.50), "p99": q(0.99), "max": xs[-1] / 1e6, "n": len(xs)}
