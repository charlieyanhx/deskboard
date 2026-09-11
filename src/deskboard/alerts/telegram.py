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
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx

from ..bus import Bus, Event
from ..engine.book import COMPONENTS
from ..engine.desk import Desk

API = "https://api.telegram.org/bot{token}/{method}"
COMMANDS = ("/risk", "/pnl", "/positions", "/alerts", "/help", "/start")
log = logging.getLogger("deskboard.telegram")


class TelegramError(RuntimeError):
    """One line, token redacted, Telegram's own description included."""


HINTS = {
    401: "token wrong (check TELEGRAM_BOT_TOKEN)",
    404: "token malformed — it looks like 123456789:AA… (check TELEGRAM_BOT_TOKEN)",
    400: "chat id wrong, or the bot is not in that group (check TELEGRAM_CHAT_ID)",
    403: "the chat has not started the bot: press Start in the private chat, or add the bot to the group",
    409: "another process is polling this token (only one deskboard --telegram per bot)",
    429: "rate limited by Telegram; the bot backs off",
}


def _explain(r: httpx.Response, method: str, token: str) -> TelegramError:
    try:
        desc = r.json().get("description", "")
    except ValueError:
        desc = ""
    hint = HINTS.get(r.status_code, "")
    return TelegramError(f"telegram {method} failed: {r.status_code} {desc}".rstrip() + (f" — {hint}" if hint else ""))


class TelegramAPI(Protocol):
    async def send_message(self, chat_id: str, text: str) -> None: ...
    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]: ...


@dataclass
class HttpTelegramAPI:
    token: str
    client: httpx.AsyncClient

    async def _call(self, method: str, timeout: float, **kw) -> dict:
        try:
            r = await self.client.post(API.format(token=self.token, method=method), timeout=timeout, **kw)
        except httpx.HTTPError as exc:  # network: never echo the URL (it carries the token)
            raise TelegramError(f"telegram {method} failed: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise _explain(r, method, self.token)
        return r.json()

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._call("sendMessage", 20, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                                  "disable_web_page_preview": True})

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        params: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return (await self._call("getUpdates", timeout + 10, json=params)).get("result", [])

    async def get_me(self) -> dict:
        return (await self._call("getMe", 20)).get("result", {})


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
            "/help": lambda: HELP, "/start": lambda: HELP}[cmd]()


# ---- the bot -------------------------------------------------------------------------

@dataclass
class TelegramBot:
    api: TelegramAPI
    chat_id: str
    desk: Desk
    sent: int = 0

    failed: int = 0

    def attach(self, bus: Bus) -> None:
        bus.subscribe("alert", self._on_alert)

    async def _on_alert(self, ev: Event) -> None:
        """A Telegram failure must never stop the book: log one line and carry on."""
        try:
            await self.send(format_alert(ev.payload))
        except TelegramError as exc:
            self.failed += 1
            log.warning("%s", exc)

    async def send(self, text: str) -> None:
        await self.api.send_message(self.chat_id, text)
        self.sent += 1

    async def handle_update(self, update: dict) -> str | None:
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
            return None                                  # not our chat: ignore silently
        reply = answer(msg.get("text", ""), self.desk)
        if reply:
            try:
                await self.send(reply)
            except TelegramError as exc:
                self.failed += 1
                log.warning("%s", exc)
        return reply

    async def poll(self, stop: asyncio.Event | None = None, timeout: int = 25, skip_backlog: bool = True) -> None:
        """Long-poll getUpdates. Messages that arrived while the bot was down are skipped
        (a stale /risk answered an hour late is worse than none). Failures are logged on
        the first occurrence and every 20th after, never silently."""
        offset = None
        if skip_backlog:
            try:
                backlog = await self.api.get_updates(None, 0)
                if backlog:
                    offset = backlog[-1]["update_id"] + 1
            except TelegramError as exc:
                log.warning("%s", exc)
        failures = 0
        while stop is None or not stop.is_set():
            try:
                updates = await self.api.get_updates(offset, timeout)
                failures = 0
            except (TelegramError, asyncio.TimeoutError) as exc:
                failures += 1
                if failures == 1 or failures % 20 == 0:
                    log.warning("getUpdates failing (%d): %s", failures, exc)
                await asyncio.sleep(min(30, 3 * failures))
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
