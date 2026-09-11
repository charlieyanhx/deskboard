"""Composition root: bus + book + limit engine, wired once, used by CLI, UI and the bot."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..bus import Bus, Event
from .book import Book
from .limits import DEMO_RULES, LimitEngine, Rule


@dataclass
class Desk:
    bus: Bus
    book: Book
    limits: LimitEngine
    alerts: list[dict] = field(default_factory=list)

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
        return desk

    async def _after_event(self, ev: Event) -> None:
        await self.limits.check(self.book.snapshot(), ev.ts)
