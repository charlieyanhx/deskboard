"""Composition root: bus + book + limit engine, wired once, used by CLI, UI and the bot."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..bus import Bus, Event
from .book import Book
from .limits import DEMO_RULES, LimitEngine, Rule
from .tape import Tape


@dataclass
class Desk:
    bus: Bus
    book: Book
    limits: LimitEngine
    alerts: list[dict] = field(default_factory=list)
    tape: Tape = field(default_factory=Tape)
    _last_checked_ts: float | None = None

    @classmethod
    def build(cls, positions: list[dict], rules: list[Rule] | None = None) -> "Desk":
        bus = Bus()
        book = Book()
        book.attach(bus)
        book.load_positions(positions)
        limits = LimitEngine(rules if rules is not None else DEMO_RULES, bus=bus)
        desk = cls(bus, book, limits)
        for topic in ("quote", "position", "fill"):
            bus.subscribe(topic, desk._after_event)
        bus.subscribe("alert", lambda ev: desk.alerts.append(ev.payload))
        desk.tape.attach(bus)          # the Live page's ring buffer; bookkeeping only
        return desk

    async def _after_event(self, ev: Event) -> None:
        # one check per event timestamp: a quote batch (spot + every leg) shares a ts, and
        # limits are read against the snapshot, so checking mid-batch buys nothing
        if ev.ts == self._last_checked_ts:
            return
        self._last_checked_ts = ev.ts
        await self.limits.check(self.book.snapshot(include_legs=False), ev.ts)
