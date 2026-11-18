"""Telegram alerts: "these posts just went live, submit them now".

Content Rewards has no API, so every post must be submitted by hand within
30 minutes. The scheduler posts in small batches and sends one message per
batch with the links, grouped by campaign, plus the campaign's submit page.

Secrets (in secrets/clipper.env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
The review bot (clipper.telegram_review) is a separate bot with its own token.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _call(method: str, params: dict, timeout: float = 15, token: str | None = None) -> dict:
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def send(text: str) -> bool:
    """Send a message; never raises (alerts must not break posting)."""
    if not configured():
        return False
    try:
        out = _call("sendMessage", {
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text[:4000],
            "disable_web_page_preview": "true",
        })
        return bool(out.get("ok"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.warning("telegram send failed: %s", exc)
        return False


def find_chat_ids(token: str | None = None) -> list[tuple[str, str]]:
    """Chats that recently messaged the bot: [(chat_id, name)]. Used once at setup."""
    out = _call("getUpdates", {"timeout": 0}, token=token)
    seen: dict[str, str] = {}
    for upd in out.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            name = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
            seen[str(chat["id"])] = name
    return list(seen.items())


def batch_message(cfg: dict, posted: list[dict], failed: list[str], now: datetime | None = None) -> str:
    """posted: [{"campaign": dict|None, "platform": str, "url": str, "clip_id": int, "hook": str}]"""
    tz = ZoneInfo(cfg["posting"].get("timezone", "UTC"))
    now = now or datetime.now(tz)
    lines: list[str] = []
    if posted:
        deadline = (now + timedelta(minutes=30)).astimezone(tz).strftime("%H:%M")
        lines.append(f"New posts live. Submit by {deadline} (30 min rule):")
        groups: dict[str, list[dict]] = {}
        for p in posted:
            key = (p["campaign"] or {}).get("id", "") or "no-campaign"
            groups.setdefault(key, []).append(p)
        for key, items in groups.items():
            camp = items[0]["campaign"]
            lines.append("")
            lines.append(camp.get("name", key) if camp else "Not in a campaign (no submission needed)")
            for p in items:
                lines.append(f"{p['url']}")
            if camp and camp.get("url"):
                lines.append(f"Submit here: {camp['url']}")
        lines.append("")
        lines.append("Tip: paste all links of a campaign into one 'Submit clip' box, one per line.")
    if failed:
        lines.append("")
        lines.append("Problems:")
        lines.extend(f"- {f}" for f in failed)
    return "\n".join(lines).strip()
