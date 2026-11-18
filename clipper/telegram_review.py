"""Review clips from your phone with a second Telegram bot.

Each clip that needs your decision arrives as a playable video with its
hook, caption, quality, spoiler check and the AI's note, plus buttons:
  ✅ Approve   ❌ Reject   ✏️ Edit in dashboard
Clips the auto-approver approved arrive with ↩️ Undo, usable during the
undo window before they post. Commands: /next (more clips), /status.

Only button presses and commands from your own chat are accepted.
Secrets (secrets/clipper.env): TELEGRAM_REVIEW_BOT_TOKEN, TELEGRAM_REVIEW_CHAT_ID
(`clipper telegram setup --review` fills the chat id).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as config_mod, gemini as gemini_mod, guardrails
from .db import DB

log = logging.getLogger(__name__)

TOKEN_ENV, CHAT_ENV = "TELEGRAM_REVIEW_BOT_TOKEN", "TELEGRAM_REVIEW_CHAT_ID"
API = "https://api.telegram.org/bot{token}/{method}"
MAX_UPLOAD = 49 * 1024 * 1024  # Bot API upload limit is 50 MB

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_messages (
    clip_id INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- 'review' (needs a decision) | 'auto' (auto-approved, undo)
    chat_id TEXT,
    message_id INTEGER,
    sent_at REAL NOT NULL,
    PRIMARY KEY (clip_id, kind)
);
"""


def configured() -> bool:
    return bool(os.environ.get(TOKEN_ENV) and os.environ.get(CHAT_ENV))


# ------------------------------------------------------------------ Bot API
def multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    """files: {name: (filename, bytes, content_type)}"""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (fname, data, ctype) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def api(method: str, params: dict | None = None, files: dict | None = None, timeout: float = 60) -> dict:
    token = os.environ.get(TOKEN_ENV, "")
    url = API.format(token=token, method=method)
    params = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in (params or {}).items()}
    if files:
        body, ctype = multipart(params, files)
    else:
        body, ctype = urllib.parse.urlencode(params).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except ValueError:
            return {"ok": False, "description": f"HTTP {exc.code}"}


# ------------------------------------------------------------------ what to send
def _notes(clip: dict) -> dict:
    try:
        return json.loads(clip.get("ai_notes") or "{}")
    except json.JSONDecodeError:
        return {}


def reviewable(cfg: dict, clip: dict) -> bool:
    """A ready clip a human could actually approve and post."""
    ch = config_mod.channel(cfg, clip["channel"])
    kind, _ = guardrails.permission(ch)
    if kind in (None, "test") or not clip.get("final_path"):
        return False
    notes = _notes(clip)
    gem = notes.get("gemini") or {}
    if clip.get("spoiler_risk") == "high" or notes.get("unsafe"):
        return False
    # hard no's never reach the phone: they'd be age-restricted, off-game or mostly violence
    if gem.get("age_risk") == "high" or gem.get("is_gta6") is False or notes.get("violence_mostly"):
        return False
    # only clips good enough to be worth your time; the rest stay on the dashboard
    if int(clip.get("quality") or 0) < int(cfg.get("review_bot", {}).get("min_quality", 6)):
        return False
    if guardrails.before_release(cfg, clip):
        return False
    if cfg.get("auto_approve", {}).get("enabled"):
        # let the auto-approver look first; send what it left for a human
        checked, updated = clip.get("auto_checked_at"), clip.get("updated_at") or 0
        if checked is None or checked < updated:
            return False
    return Path(clip["final_path"]).exists()


def pending_review(db: DB, cfg: dict, limit: int) -> list[dict]:
    db.conn.executescript(SCHEMA)
    rows = db.query(
        "SELECT c.* FROM clips c LEFT JOIN review_messages m ON m.clip_id = c.id AND m.kind = 'review' "
        "WHERE c.status = 'ready' AND m.clip_id IS NULL ORDER BY c.quality DESC, c.score DESC, c.id DESC")
    out = []
    for r in rows:
        d = dict(r)
        if reviewable(cfg, d):
            out.append(d)
            if len(out) >= limit:
                break
    return out


def auto_approved_unnotified(db: DB, cfg: dict) -> list[dict]:
    db.conn.executescript(SCHEMA)
    veto = float(cfg.get("auto_approve", {}).get("veto_minutes", 10)) * 60
    rows = db.query(
        "SELECT c.* FROM clips c LEFT JOIN review_messages m ON m.clip_id = c.id AND m.kind = 'auto' "
        "WHERE c.status = 'approved' AND c.decided_by = 'auto' AND c.decided_at > ? AND m.clip_id IS NULL",
        (time.time() - veto,))
    return [dict(r) for r in rows]


def caption_for(cfg: dict, clip: dict, kind: str) -> str:
    ch = config_mod.channel(cfg, clip["channel"]) or {}
    camp = guardrails.campaign_for(cfg, ch)
    name = ch.get("display_name") or clip["channel"]
    notes = _notes(clip)
    gem = notes.get("gemini") or {}
    score = f"quality {clip.get('quality') or 0}/10"
    if gem:
        score += f" (Claude {notes.get('claude_quality')} · {gemini_mod.label(gem)} {gem.get('quality')})"
        if notes.get("tiebreak"):
            score += " · views broke the tie"
    lines = [f"#{clip['id']} {name} · {score} · spoiler {clip.get('spoiler_risk')}"]
    if camp:
        lines.append(camp.get("name", camp.get("id", "")))
    if kind == "auto":
        veto = int(cfg.get("auto_approve", {}).get("veto_minutes", 10))
        lines.append(f"Auto-approved: posts in about {veto} min unless you undo.")
    if clip.get("source_note"):
        lines.append(clip["source_note"][:200])
    lines.append(f"Hook: {clip.get('hook', '').strip()}")
    if notes.get("violence_cut"):
        k = notes["violence_cut"].get("kept", [0, 0])
        what = ", ".join(sorted({str(v[2]) for v in notes["violence_cut"].get("removed", [])}))[:80]
        lines.append(f"✂️ Cut out ({what}): kept {k[0]:.0f}-{k[1]:.0f}s of {notes['violence_cut'].get('was', 0):.0f}s")
    if notes.get("music_heavy") or gem.get("music") in ("possible", "likely"):
        lines.append(f"🎵 Licensed music: {gem.get('music') if gem.get('music') != 'none' else 'heavy'}"
                     + (f" ({gem['music_reason']})" if gem.get("music_reason") else ""))
    if notes.get("hook_swapped"):
        lines.append(f"({gemini_mod.label(gem)}'s hook; the first one, \"{notes['hook_swapped'].get('from', '')}\", "
                     "didn't match the video)")
    lines.append(f"Caption: {(clip.get('caption') or '').strip()[:350]}")
    if gem.get("what_happens"):
        lines.append(f"{gemini_mod.label(gem)} saw: {gem['what_happens'][:220]}")
    if gem.get("better_hook"):
        lines.append(f"{gemini_mod.label(gem)} hook idea: {gem['better_hook']}")
    if gem.get("age_risk") in ("low", "high"):
        lines.append(f"Age-restriction risk: {gem['age_risk']}" + (f" ({gem['age_reason']})" if gem.get("age_reason") else ""))
    if gem.get("is_gta6") is False:
        lines.append(f"⚠️ {gemini_mod.label(gem)} doesn't think this is GTA 6 gameplay")
    why = (clip.get("decision_note") or notes.get("why") or "").strip()
    if why:
        lines.append(f"AI: {why[:260]}")
    return "\n".join(lines)[:1024]


def keyboard(cfg: dict, clip_id: int, kind: str) -> dict:
    if kind == "auto":
        return {"inline_keyboard": [[{"text": "↩️ Undo approval", "callback_data": f"u:{clip_id}"}]]}
    rows = [[{"text": "✅ Approve", "callback_data": f"a:{clip_id}"},
             {"text": "❌ Reject", "callback_data": f"r:{clip_id}"}]]
    url = cfg.get("review_bot", {}).get("dashboard_url")
    if url:
        rows.append([{"text": "✏️ Edit in dashboard", "url": url.rstrip("/") + "/"}])
    return {"inline_keyboard": rows}


def send_clip(db: DB, cfg: dict, clip: dict, kind: str) -> bool:
    chat = os.environ.get(CHAT_ENV, "")
    caption = caption_for(cfg, clip, kind)
    markup = keyboard(cfg, clip["id"], kind)
    video = Path(clip["final_path"])
    if video.exists() and video.stat().st_size <= MAX_UPLOAD:
        out = api("sendVideo", {
            "chat_id": chat, "caption": caption, "reply_markup": markup,
            "supports_streaming": "true", "width": 1080, "height": 1920,
            "duration": int(float(clip.get("duration_s") or 0)),
        }, files={"video": (f"clip{clip['id']}.mp4", video.read_bytes(), "video/mp4")}, timeout=180)
    else:
        out = api("sendMessage", {"chat_id": chat, "text": caption + "\n(video too large to preview)",
                                  "reply_markup": markup})
    if not out.get("ok"):
        log.warning("review bot: sending clip %s failed: %s", clip["id"], out.get("description"))
        return False
    db.execute("INSERT OR REPLACE INTO review_messages (clip_id, kind, chat_id, message_id, sent_at) "
               "VALUES (?, ?, ?, ?, ?)", (clip["id"], kind, chat, out["result"]["message_id"], time.time()))
    return True


# ------------------------------------------------------------------ decisions
def decide(db: DB, cfg: dict, action: str, clip_id: int) -> str:
    """Apply a button press. Returns the short result shown to you."""
    row = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    if not row:
        return "Clip not found"
    clip, now = dict(row), time.time()
    status = clip["status"]
    if action == "a":
        if status == "approved":
            return "Already approved"
        if status not in ("ready", "rejected", "failed") or not clip["final_path"]:
            return f"Can't approve: clip is {status}"
        if clip.get("spoiler_risk") == "high":
            return "Blocked: flagged as a story spoiler"
        if why := guardrails.before_release(cfg, clip):
            return f"Blocked: {why}"
        db.update("clips", clip_id, status="approved", decided_by="human", decided_at=now,
                  decision_note="Approved in Telegram")
        return "✅ Approved: posting within a minute"
    if action == "r":
        if status in ("posted", "rendering"):
            return f"Can't reject: clip is {status}"
        db.update("clips", clip_id, status="rejected", decided_by="human", decided_at=now,
                  decision_note="Rejected in Telegram")
        return "❌ Rejected"
    if action == "u":
        if status != "approved":
            return f"Too late: clip is {status}"
        if db.one("SELECT id FROM posts WHERE clip_id = ? AND status != 'error'", (clip_id,)):
            return "Too late: already posted"
        db.update("clips", clip_id, status="ready", decided_by="human", decided_at=now,
                  decision_note="Approval undone in Telegram")
        return "↩️ Undone: it won't post"
    return "Unknown action"


def status_text(db: DB, cfg: dict) -> str:
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM clips GROUP BY status")}
    waiting = len(pending_review(db, cfg, 1000))
    posts_today = db.one("SELECT COUNT(*) n FROM posts WHERE posted_at > ? AND status != 'error'",
                         (time.time() - 86400,))["n"]
    return (f"Waiting for you: {waiting} clips\n"
            f"Approved, about to post: {counts.get('approved', 0)}\n"
            f"Posts in the last 24h: {posts_today}\n"
            f"Ready (incl. ones blocked as spoilers): {counts.get('ready', 0)}\n"
            "Send /next for more clips.")


HELP = ("I send clips that need your decision. Tap ✅ Approve and the clip posts to its "
        "streamer's YouTube and Instagram within a minute, and the alerts bot sends you the links to submit.\n"
        "/next: send more clips now\n/status: queue summary")


def handle_update(db: DB, cfg: dict, upd: dict) -> None:
    chat_ok = os.environ.get(CHAT_ENV, "")
    if cb := upd.get("callback_query"):
        msg = cb.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != chat_ok:
            api("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Not allowed"})
            return
        try:
            action, cid = cb.get("data", "").split(":", 1)
            result = decide(db, cfg, action, int(cid))
        except ValueError:
            result = "Unknown button"
        api("answerCallbackQuery", {"callback_query_id": cb["id"], "text": result[:190]})
        done = result.startswith(("✅", "❌", "↩️")) or result.startswith(("Already", "Too late", "Can't", "Blocked"))
        if done and msg.get("message_id"):
            new_caption = ((msg.get("caption") or msg.get("text") or "") + f"\n\n{result}")[:1024]
            method = "editMessageCaption" if "caption" in msg else "editMessageText"
            field = "caption" if "caption" in msg else "text"
            api(method, {"chat_id": chat_ok, "message_id": msg["message_id"], field: new_caption,
                         "reply_markup": {"inline_keyboard": []}})
        return
    msg = upd.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != chat_ok:
        return  # ignore strangers entirely
    text = (msg.get("text") or "").strip().lower()
    if text.startswith("/next"):
        sent = push(db, cfg, force=True, limit=5)
        if not sent:
            api("sendMessage", {"chat_id": chat_ok, "text": "Nothing waiting for review right now."})
    elif text.startswith("/status"):
        api("sendMessage", {"chat_id": chat_ok, "text": status_text(db, cfg)})
    else:
        api("sendMessage", {"chat_id": chat_ok, "text": HELP})


# ------------------------------------------------------------------ loop
def _quiet_now(cfg: dict) -> bool:
    posting = cfg["posting"]
    hour = datetime.now(ZoneInfo(posting.get("timezone", "UTC"))).hour
    return hour in set(posting.get("quiet_hours", []))


def push(db: DB, cfg: dict, force: bool = False, limit: int | None = None) -> int:
    """Send undo notices for auto-approvals, then clips that need a decision."""
    rcfg = cfg["review_bot"]
    sent = 0
    for clip in auto_approved_unnotified(db, cfg):
        sent += send_clip(db, cfg, clip, "auto")
    if _quiet_now(cfg) and not force:
        return sent
    recent = db.one("SELECT COUNT(*) n FROM review_messages WHERE kind = 'review' AND sent_at > ?",
                    (time.time() - 3600,))["n"]
    room = int(rcfg.get("max_per_hour", 12)) - recent
    if force:
        room = max(room, limit or 5)
    batch = min(room, limit or int(rcfg.get("per_push", 3)))
    if batch <= 0:
        return sent
    for clip in pending_review(db, cfg, batch):
        sent += send_clip(db, cfg, clip, "review")
    return sent


class ReviewBot(threading.Thread):
    def __init__(self, db: DB, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name="review-bot")
        self.db, self.cfg, self.stop_event = db, cfg, stop

    def run(self) -> None:
        self.db.conn.executescript(SCHEMA)
        offset, last_push = 0, 0.0
        every = float(self.cfg["review_bot"].get("push_every_seconds", 60))
        while not self.stop_event.is_set():
            try:
                if time.time() - last_push >= every:
                    push(self.db, self.cfg)
                    last_push = time.time()
                out = api("getUpdates", {"timeout": 20, "offset": offset,
                                         "allowed_updates": ["callback_query", "message"]}, timeout=35)
                for upd in out.get("result", []):
                    offset = upd["update_id"] + 1
                    try:
                        handle_update(self.db, self.cfg, upd)
                    except Exception:
                        log.exception("review bot: update failed")
            except Exception:
                log.exception("review bot loop error")
                self.stop_event.wait(15)
