"""A "✅ Submitted" button on each post alert, and a reminder before the 30-minute window closes.

Content Rewards submissions stay manual (no official API for clippers; bots break its terms and
Reach's one-strike rule). This only makes the manual step hard to miss: one button per campaign on
the alert, and if it isn't tapped within remind_after_minutes, a reply on the alert saying how long
is left. Button taps are accepted only from your own chat (TELEGRAM_CHAT_ID).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

from . import notify
from .db import DB

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    campaign_id TEXT DEFAULT '',
    campaign_name TEXT DEFAULT '',
    urls TEXT DEFAULT '[]',
    chat_id TEXT DEFAULT '',
    message_id INTEGER,
    sent_at REAL NOT NULL,
    deadline REAL NOT NULL,
    submitted_at REAL,
    reminded_at REAL
);
"""


def _short(name: str) -> str:
    """'$12,500 LACY GTA VI Marathon Pool (Reach)' -> 'LACY GTA VI Marathon Pool'."""
    words = [w for w in name.replace("(Reach)", "").split() if not w.startswith("$")]
    return " ".join(words)[:40] or "campaign"


def _clock(cfg: dict, ts: float) -> str:
    tz = ZoneInfo(cfg["posting"].get("timezone", "UTC"))
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def keyboard(cfg: dict, db: DB, message_id: int) -> dict:
    rows = []
    for s in db.query("SELECT * FROM submissions WHERE message_id = ? ORDER BY id", (message_id,)):
        if s["submitted_at"]:
            rows.append([{"text": f"✔ {_short(s['campaign_name'])}: submitted {_clock(cfg, s['submitted_at'])}",
                          "callback_data": "noop"}])
        else:
            rows.append([{"text": f"✅ Submitted: {_short(s['campaign_name'])}", "callback_data": f"sub:{s['id']}"}])
    return {"inline_keyboard": rows}


def send_batch(cfg: dict, db: DB, posted: list[dict], failed: list[str], now: float | None = None) -> bool:
    """The post alert (links grouped by campaign, submit page, deadline) with a button per campaign."""
    db.conn.executescript(SCHEMA)
    now = now or time.time()
    text = notify.batch_message(cfg, posted, failed, datetime.fromtimestamp(now, ZoneInfo(cfg["posting"].get("timezone", "UTC"))))
    if not notify.configured():
        return False
    window = float(cfg["posting"].get("submit_minutes", 30)) * 60
    groups: dict[str, dict] = {}
    for p in posted:
        camp = p.get("campaign")
        if camp and camp.get("id"):
            g = groups.setdefault(camp["id"], {"name": camp.get("name", camp["id"]), "urls": []})
            g["urls"].append(p["url"])
    ids = [db.insert("submissions", campaign_id=cid, campaign_name=g["name"], urls=json.dumps(g["urls"]),
                     sent_at=now, deadline=now + window) for cid, g in groups.items()]
    params = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000], "disable_web_page_preview": "true"}
    if ids:
        params["reply_markup"] = json.dumps({"inline_keyboard": [
            [{"text": f"✅ Submitted: {_short(g['name'])}", "callback_data": f"sub:{i}"}]
            for i, g in zip(ids, groups.values())]})
    try:
        out = notify._call("sendMessage", params)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.warning("post alert failed: %s", exc)
        return False
    msg = (out or {}).get("result") or {}
    for i in ids:
        db.execute("UPDATE submissions SET message_id = ?, chat_id = ? WHERE id = ?",
                   (msg.get("message_id"), str((msg.get("chat") or {}).get("id", "")), i))
    return bool(out.get("ok"))


def handle_update(cfg: dict, db: DB, upd: dict, now: float | None = None) -> str:
    """A button tap: mark that campaign submitted and redraw the buttons. Returns what happened."""
    cq = upd.get("callback_query") or {}
    if not cq:
        return ""
    msg = cq.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    if chat != str(os.environ.get("TELEGRAM_CHAT_ID", "")):
        return "ignored: not your chat"
    data = str(cq.get("data", ""))
    answer = ""
    if data.startswith("resume:") and data.count(":") >= 2:
        from . import takedowns

        _, platform, channel = data.split(":", 2)
        answer = takedowns.resume_channel(cfg, db, channel, platform)
        try:
            notify._call("editMessageReplyMarkup", {
                "chat_id": chat, "message_id": msg.get("message_id"),
                "reply_markup": json.dumps({"inline_keyboard": [[{
                    "text": f"▶️ {answer[:1].upper() + answer[1:]} ({_clock(cfg, now or time.time())})",
                    "callback_data": "noop"}]]})})
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            log.warning("couldn't update the resume button: %s", exc)
    elif data.startswith("sub:") and data[4:].isdigit():
        sid = int(data[4:])
        row = db.one("SELECT * FROM submissions WHERE id = ?", (sid,))
        if row and not row["submitted_at"]:
            now = now or time.time()
            db.execute("UPDATE submissions SET submitted_at = ? WHERE id = ?", (now, sid))
            late = now > row["deadline"]
            answer = f"{_short(row['campaign_name'])} marked submitted" + (" (after the 30 min window)" if late else "")
            try:
                notify._call("editMessageReplyMarkup", {
                    "chat_id": chat, "message_id": msg.get("message_id"),
                    "reply_markup": json.dumps(keyboard(cfg, db, msg.get("message_id")))})
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                log.warning("couldn't update the buttons: %s", exc)
    try:
        notify._call("answerCallbackQuery", {"callback_query_id": cq.get("id", ""), "text": answer[:190]})
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        pass
    return answer or "nothing to do"


def remind_due(cfg: dict, db: DB, now: float | None = None) -> int:
    """Reply on alerts whose campaign isn't marked submitted remind_after_minutes in."""
    db.conn.executescript(SCHEMA)
    now = now or time.time()
    after = float(cfg["posting"].get("remind_after_minutes", 20)) * 60
    sent = 0
    for s in db.query("SELECT * FROM submissions WHERE submitted_at IS NULL AND reminded_at IS NULL "
                      "AND message_id IS NOT NULL AND sent_at <= ? AND deadline > ?", (now - after, now)):
        left = max(1, round((s["deadline"] - now) / 60))
        try:
            notify._call("sendMessage", {
                "chat_id": s["chat_id"] or os.environ.get("TELEGRAM_CHAT_ID", ""),
                "reply_to_message_id": s["message_id"],
                "text": f"⏰ {_short(s['campaign_name'])}: {left} min left to submit (links above). "
                        "Tap ✅ there once it's in.",
            })
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            log.warning("submit reminder failed: %s", exc)
            continue
        db.execute("UPDATE submissions SET reminded_at = ? WHERE id = ?", (now, s["id"]))
        sent += 1
    return sent


class SubmitTracker(threading.Thread):
    """Listens for ✅ taps on the alerts bot and sends the reminders."""

    def __init__(self, db: DB, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name="submit-tracker")
        self.db, self.cfg, self.stop_event = db, cfg, stop

    def run(self) -> None:
        self.db.conn.executescript(SCHEMA)
        offset = 0
        while not self.stop_event.is_set():
            try:
                out = notify._call("getUpdates", {"offset": offset, "timeout": 25,
                                                  "allowed_updates": json.dumps(["callback_query"])}, timeout=40)
                for upd in out.get("result", []):
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                    what = handle_update(self.cfg, self.db, upd)
                    if what:
                        log.info("submit tracker: %s", what)
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                log.warning("submit tracker: %s", exc)
                self.stop_event.wait(10)
            try:
                remind_due(self.cfg, self.db)
            except Exception:
                log.exception("submit reminders failed")
