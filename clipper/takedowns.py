"""Watch posted clips for takedowns, and stop posting to an account after its first one.

Rockstar's policy allows its footage only for non-commercial use, so paid clipping runs on
Rockstar's tolerance: Take-Two can have any video taken down, and on YouTube three copyright strikes
delete the channel. Every few hours each published post is checked through the official APIs:

- removed (gone from YouTube/Instagram) or rejected by YouTube (copyright, terms, claim...):
  the account is PAUSED (no more posts to it) and you get a Telegram alert with a "Resume posting"
  button (or run `clipper resume <streamer> <platform>` on the server) for once you've looked. If
  you deleted the post yourself, just resume.
- blocked in some countries (usually a music/Content ID claim) or age-restricted: alert only.

Each finding is reported once.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import config as config_mod
from .db import DB

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS takedowns (
    post_id INTEGER PRIMARY KEY,
    platform TEXT, account TEXT, channel TEXT, url TEXT,
    kind TEXT, detail TEXT, detected_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS paused_accounts (
    account TEXT PRIMARY KEY,
    platform TEXT, channel TEXT, reason TEXT, paused_at REAL NOT NULL
);
"""
PAUSE_KINDS = ("removed", "rejected")


def _schema(db: DB) -> None:
    db.conn.executescript(SCHEMA)


def paused(db: DB, account: str) -> str:
    """Why posting to this account is paused, or "" if it isn't."""
    _schema(db)
    row = db.one("SELECT reason FROM paused_accounts WHERE account = ?", (account,))
    return row["reason"] if row else ""


def resume(db: DB, account: str) -> bool:
    _schema(db)
    return db.execute("DELETE FROM paused_accounts WHERE account = ?", (account,)).rowcount > 0


def resume_channel(cfg: dict, db: DB, channel: str, platform: str) -> str:
    from .publishers import channel_account

    ch = config_mod.channel(cfg, channel)
    account = channel_account(cfg, ch, platform) if ch else None
    if not account:
        return f"no {platform} account for {channel}"
    name = (ch or {}).get("display_name") or channel
    if resume(db, account):
        return f"posting to {name} {platform} resumed"
    return f"{name} {platform} wasn't paused"


def _send_with_resume(text: str, channel: str, platform: str, name: str) -> bool:
    """The pause alert, with a button that resumes posting (handled by submit_tracker's listener)."""
    from . import notify

    if not notify.configured():
        return False
    try:
        out = notify._call("sendMessage", {
            "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000], "disable_web_page_preview": "true",
            "reply_markup": json.dumps({"inline_keyboard": [[{
                "text": f"▶️ Resume posting: {name} {platform}", "callback_data": f"resume:{platform}:{channel}"[:64]}]]}),
        })
        return bool(out.get("ok"))
    except Exception as exc:
        log.warning("pause alert failed: %s", exc)
        return notify.send(text)


def run_once(db: DB, cfg: dict, send=None, send_pause=None) -> list[str]:
    from . import notify
    from .publishers import get_publisher

    from .publishers.base import status_many

    send = send or notify.send
    send_pause = send_pause or _send_with_resume
    _schema(db)
    lines: list[str] = []
    posts = db.query("SELECT p.*, c.channel FROM posts p JOIN clips c ON c.id = p.clip_id "
                     "WHERE p.status = 'published' AND p.remote_id != '' AND p.platform IN ('youtube', 'instagram') "
                     "AND p.id NOT IN (SELECT post_id FROM takedowns)")
    # one batch per account: YouTube checks 50 videos per quota unit instead of one
    groups: dict[tuple[str, str], list] = {}
    for post in posts:
        groups.setdefault((post["platform"], post["channel"]), []).append(post)
    states: dict[int, tuple[str, str]] = {}
    for (platform, channel), group in groups.items():
        ch = config_mod.channel(cfg, channel) or {"login": channel}
        try:
            found = status_many(get_publisher(platform, cfg, ch), [p["remote_id"] for p in group])
        except Exception as exc:
            log.warning("takedown check %s/%s failed: %s", platform, channel, exc)
            continue
        states.update({p["id"]: found.get(p["remote_id"], ("unknown", "")) for p in group})
    for post in posts:
        ch = config_mod.channel(cfg, post["channel"]) or {"login": post["channel"]}
        state, detail = states.get(post["id"], ("unknown", ""))
        if state in ("ok", "unknown"):
            continue
        db.execute("INSERT OR IGNORE INTO takedowns (post_id, platform, account, channel, url, kind, detail, detected_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (post["id"], post["platform"], post["account"], post["channel"], post["url"], state, detail, time.time()))
        name = ch.get("display_name") or post["channel"]
        where = f"{name} {post['platform'].capitalize()}"
        if state in PAUSE_KINDS:
            db.execute("INSERT OR REPLACE INTO paused_accounts (account, platform, channel, reason, paused_at) "
                       "VALUES (?, ?, ?, ?, ?)",
                       (post["account"], post["platform"], post["channel"], f"{state}: {detail}", time.time()))
            db.execute("UPDATE posts SET status = ? WHERE id = ?", (state, post["id"]))
            text = (f"🛑 {where}: clip #{post['clip_id']} was {state} ({detail}).\n{post['url']}\n"
                    f"Posting to this account is PAUSED so a takedown can't turn into more strikes. Check it "
                    f"(YouTube Studio → Content, or the Instagram app), then tap Resume below to post again "
                    f"(or on the server: clipper resume {post['channel']} {post['platform']}).")
            send_pause(text, post["channel"], post["platform"], name)
        else:
            text = f"⚠️ {where}: clip #{post['clip_id']} is {detail}.\n{post['url']}\nPosting continues."
            send(text)
        line = f"{where}: clip {post['clip_id']} {state} ({detail})" + (" -> account paused" if state in PAUSE_KINDS else "")
        log.warning("takedowns: %s", line)
        lines.append(line)
    return lines


class TakedownWatcher(threading.Thread):
    def __init__(self, db: DB, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name="takedowns")
        self.db, self.cfg, self.stop_event = db, cfg, stop

    def run(self) -> None:
        hours = float(self.cfg["posting"].get("takedown_check_hours", 3))
        self.stop_event.wait(120)
        while not self.stop_event.is_set():
            try:
                run_once(self.db, self.cfg)
            except Exception:
                log.exception("takedown check failed")
            self.stop_event.wait(hours * 3600)
