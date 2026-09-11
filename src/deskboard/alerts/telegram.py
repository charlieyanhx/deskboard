"""Telegram alert channel and command bot.

Outbound: every `alert` event on the bus becomes one message (BREACH or CLEARED, with the
reason). Inbound: long-polls `getUpdates` and answers `/risk`, `/pnl`, `/positions`,
`/alerts`, `/help` from the current snapshot — only for the configured chat id; anything
else is ignored. The dashboard never submits orders, and neither does the bot.

Configuration is environment only (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`); nothing is
read from the repo. The Bot API is called directly over HTTPS with httpx — no framework.
"""

from __future__ import annotations

import asyncio
import html
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx

from ..bus import Bus, Event
from ..engine.book import COMPONENTS
from ..engine.desk import Desk

API = "https://api.telegram.org/bot{token}/{method}"
COMMANDS = ("/risk", "/pnl", "/positions", "/alerts", "/help")


class TelegramAPI(Protocol):
    async def send_message(self, chat_id: str, text: str) -> None: ...
    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]: ...


@dataclass
class HttpTelegramAPI:
    token: str
    client: httpx.AsyncClient

    async def send_message(self, chat_id: str, text: str) -> None:
        r = await self.client.post(API.format(token=self.token, method="sendMessage"),
                                   json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                         "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        r = await self.client.get(API.format(token=self.token, method="getUpdates"), params=params, timeout=timeout + 10)
        r.raise_for_status()
        return r.json().get("result", [])


# ---- formatting (pure) ----------------------------------------------------------------

def _ts(ts: float | None) -> str:
    return "—" if not ts else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S UTC")


def format_alert(a: dict) -> str:
    icon = "🔴" if a["state"] == "BREACH" else "🟢"
    where = "book" if a["target"] == "book" else f"position <code>{html.escape(a['target'])}</code>"
    return f"{icon} <b>{a['state']}</b> {html.escape(a['rule'])} on {where}\n{html.escape(a['reason'])}\n<i>{_ts(a['ts'])}</i>"


def format_risk(snap: dict) -> str:
    t = snap["totals"]
    spot = ", ".join(f"{k} {v:,.2f}" for k, v in t["spot"].items()) or "—"
    return (f"<b>Risk</b> · {_ts(t['last_ts'])} · spot {spot}\n"
            f"P&amp;L today <b>{t['pnl']:+,.0f}</b>\n"
            f"net Δ$ {t['delta_usd']:+,.0f}\nΓ$ per 1% {t['gamma_usd_1pct']:+,.0f}\n"
            f"ν$ per vol pt {t['vega_usd_1vol']:+,.0f}\nΘ$ per day {t['theta_usd_day']:+,.0f}\n"
            f"positions {t['n_positions']} · identity gap {t['identity_gap']:.4f}")


def format_pnl(snap: dict) -> str:
    t = snap["totals"]
    rows = "\n".join(f"{c:<10} {t[f'pnl_{c}']:+,.0f}" for c in COMPONENTS)
    return f"<b>P&amp;L attribution today</b> · {_ts(t['last_ts'])}\n<pre>{rows}\n{'total':<10} {t['pnl']:+,.0f}</pre>"


def format_positions(snap: dict) -> str:
    if not snap["positions"]:
        return "no positions"
    rows = "\n".join(f"{r['pos_id']:<8} {r['sleeve']:<2} Δ$ {r['delta_usd']:+9,.0f}  P&L {r['pnl']:+8,.0f}"
                     for r in snap["positions"])
    return f"<b>Positions</b>\n<pre>{html.escape(rows)}</pre>"


def format_alerts(history: list[dict], n: int = 8) -> str:
    if not history:
        return "no alerts this session"
    rows = "\n".join(f"{_ts(a['ts'])} {a['state']:<7} {a['rule']} {a['target']}: {a['reason']}" for a in history[-n:])
    return f"<b>Last {min(n, len(history))} alerts</b>\n<pre>{html.escape(rows)}</pre>"


HELP = ("<b>deskboard</b> — read-only. Commands:\n/risk — book Greeks and P&amp;L\n/pnl — attribution\n"
        "/positions — per position\n/alerts — recent limit events\n/help")


def answer(command: str, desk: Desk) -> str | None:
    cmd = command.strip().split()[0].split("@")[0].lower() if command.strip() else ""
    if cmd not in COMMANDS:
        return None
    snap = desk.book.snapshot()
    return {"/risk": lambda: format_risk(snap), "/pnl": lambda: format_pnl(snap),
            "/positions": lambda: format_positions(snap), "/alerts": lambda: format_alerts(desk.alerts),
            "/help": lambda: HELP}[cmd]()


# ---- the bot -------------------------------------------------------------------------

@dataclass
class TelegramBot:
    api: TelegramAPI
    chat_id: str
    desk: Desk
    sent: int = 0

    def attach(self, bus: Bus) -> None:
        bus.subscribe("alert", self._on_alert)

    async def _on_alert(self, ev: Event) -> None:
        await self.send(format_alert(ev.payload))

    async def send(self, text: str) -> None:
        await self.api.send_message(self.chat_id, text)
        self.sent += 1

    async def handle_update(self, update: dict) -> str | None:
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
            return None                                  # not our chat: ignore silently
        reply = answer(msg.get("text", ""), self.desk)
        if reply:
            await self.send(reply)
        return reply

    async def poll(self, stop: asyncio.Event | None = None, timeout: int = 25) -> None:
        offset = None
        while stop is None or not stop.is_set():
            try:
                updates = await self.api.get_updates(offset, timeout)
            except (httpx.HTTPError, asyncio.TimeoutError):
                await asyncio.sleep(3)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                await self.handle_update(u)
            if not updates:
                await asyncio.sleep(0.01)   # yield even when the API answers instantly (tests, fakes)


def from_env(desk: Desk, client: httpx.AsyncClient | None = None) -> TelegramBot | None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return None
    return TelegramBot(HttpTelegramAPI(token, client or httpx.AsyncClient()), chat, desk)
