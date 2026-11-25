"""Morning Telegram digest: the whole operation in one message, once a day.

Last 24 hours of posts and the views they gained (against a snapshot from the previous digest),
each pool campaign's progress toward its total-views threshold, what the clipper made and why
clips were held, submissions, Claude spend, takedowns, and the best clips waiting for you.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config as config_mod
from .db import DB

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS digest_log (
    day TEXT PRIMARY KEY,           -- local date the digest was sent for
    sent_at REAL NOT NULL,
    views_json TEXT DEFAULT '{}'    -- {post_id: views} at send time, for tomorrow's deltas
);
"""


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg["posting"].get("timezone", "UTC"))


PLATFORM = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok", "x": "X"}


def _p(platform: str) -> str:
    return PLATFORM.get(platform, platform.capitalize())


def _n(x: int | float) -> str:
    return f"{int(x):,}"


def _reason(note: str) -> str:
    """Short reason a clip didn't post, from its decision note."""
    note = note or ""
    if not note:
        return "not reviewed yet"
    if " | held: " in note:
        note = note.split(" | held: ")[-1]
    for prefix in ("Needs a human: ", "Below the bar, not judged: "):
        note = note.replace(prefix, "")
    for key, label in (("Weak moment", "weak moment"), ("Stale", "stale in queue"), ("unsafe", "unsafe"),
                       ("spoiler", "spoiler risk"), ("hook doesn't match", "hook mismatch"),
                       ("age-restrict", "18+ risk"), ("violence", "violence"), ("licensed music", "licensed music"),
                       ("cold-start bar", "under quality bar"), ("rates it", "video check score low"),
                       ("Expired", "expired unposted"), ("AI hold", "judge unsure"), ("AI reject", "judge rejected"),
                       ("not GTA 6", "not GTA 6")):
        if key in note:
            return label
    return note[:40] or "other"


def _prev_snapshot(db: DB, today: str) -> dict:
    row = db.one("SELECT views_json FROM digest_log WHERE day < ? ORDER BY day DESC LIMIT 1", (today,))
    try:
        return {int(k): int(v) for k, v in json.loads(row["views_json"] if row else "{}").items()}
    except (ValueError, TypeError):
        return {}


def build(db: DB, cfg: dict, now: float | None = None) -> tuple[str, dict]:
    """(message, {post_id: views} snapshot)."""
    from . import ai, submit_tracker, takedowns

    db.conn.executescript(SCHEMA)
    db.conn.executescript(submit_tracker.SCHEMA)
    takedowns._schema(db)
    now = now or time.time()
    since = now - 86400
    local = datetime.fromtimestamp(now, _tz(cfg))
    today = local.strftime("%Y-%m-%d")
    prev = _prev_snapshot(db, today)
    posts = [dict(r) for r in db.query(
        "SELECT p.*, c.channel, c.hook FROM posts p JOIN clips c ON c.id = p.clip_id WHERE p.status != 'error'")]
    snapshot = {p["id"]: int(p["views"] or 0) for p in posts}
    name = lambda login: (config_mod.channel(cfg, login) or {}).get("display_name") or login  # noqa: E731
    lines = [f"☀️ GTA 6 Clipper: {local:%a %d %b} (last 24 h)", ""]

    # posts and views
    new = [p for p in posts if (p["posted_at"] or 0) > since]
    by_platform = Counter(p["platform"] for p in new)
    lines.append(f"Posts: {len(new)}" + (" (" + ", ".join(f"{_p(k)} {v}" for k, v in by_platform.items()) + ")"
                                          if new else ""))
    new_ids = {p["id"] for p in new}
    if prev:
        gains = {p["id"]: max(0, snapshot[p["id"]] - prev.get(p["id"], 0)) for p in posts}
    else:  # first digest: only new posts' views are known to be from the last 24 h
        gains = {p["id"]: snapshot[p["id"]] if p["id"] in new_ids else 0 for p in posts}
    gp = Counter()
    for p in posts:
        gp[p["platform"]] += gains[p["id"]]
    lines.append(f"Views gained: +{_n(sum(gains.values()))}"
                 + (" (" + ", ".join(f"{_p(k)} +{_n(v)}" for k, v in gp.most_common()) + ")" if gp else "")
                 + ("" if prev else ", new posts only (older posts' gains show from tomorrow)"))
    if posts:
        top = max(posts, key=lambda p: (gains[p["id"]], snapshot[p["id"]]))
        lines.append(f"Top: {name(top['channel'])} #{top['clip_id']} {_p(top['platform'])} "
                     f"{_n(snapshot[top['id']])} views (+{_n(gains[top['id']])}) {top['url']}")

    # pool progress
    pool_lines = []
    for camp in cfg.get("campaigns", []):
        need = int(camp.get("pool_min_views") or 0)
        if not camp.get("active") or not need:
            continue
        got = sum(snapshot[p["id"]] for p in posts if p.get("campaign_id") == camp["id"])
        short = submit_tracker._short(camp.get("name", camp["id"]))
        pool_lines.append(f"• {short}: {_n(got)} / {_n(need)} ({got / need:.1%})")
    if pool_lines:
        lines += ["", "Pools (views on our posts / total needed to share the pool):", *pool_lines]

    # what the clipper did
    made = db.one("SELECT COUNT(*) n FROM clips WHERE created_at > ?", (since,))["n"]
    auto = db.one("SELECT COUNT(*) n FROM clips WHERE decided_by = 'auto' AND status IN ('approved', 'posted') "
                  "AND decided_at > ?", (since,))["n"]
    you_ok = db.one("SELECT COUNT(*) n FROM clips WHERE decided_by = 'human' AND status IN ('approved', 'posted') "
                    "AND decided_at > ?", (since,))["n"]
    you_no = db.one("SELECT COUNT(*) n FROM clips WHERE decided_by = 'human' AND status = 'rejected' "
                    "AND decided_at > ?", (since,))["n"]
    held = Counter(_reason(r["decision_note"]) for r in db.query(
        "SELECT decision_note FROM clips WHERE created_at > ? AND status IN ('ready', 'rejected') "
        "AND COALESCE(decided_by, '') != 'human'", (since,)))
    lines += ["", f"Clips: {_n(made)} made · {auto} auto-approved · you approved {you_ok}, rejected {you_no}"]
    if held:
        lines.append("Not posted mostly for: " + ", ".join(f"{k} {v}" for k, v in held.most_common(4)))

    # submissions
    subs = [dict(r) for r in db.query("SELECT * FROM submissions WHERE sent_at > ?", (since,))]
    if subs:
        done = sum(1 for s in subs if s["submitted_at"] and s["submitted_at"] <= s["deadline"])
        late = sum(1 for s in subs if s["submitted_at"] and s["submitted_at"] > s["deadline"])
        missed = sum(1 for s in subs if not s["submitted_at"] and s["deadline"] < now)
        lines.append(f"Submissions: {len(subs)} to do · {done} on time" + (f" · {late} late" if late else "")
                     + (f" · {missed} missed" if missed else ""))

    # spend
    conn = ai._db(cfg)
    try:
        spend = dict(conn.execute("SELECT purpose, SUM(cost_usd) FROM ai_usage WHERE ts > ? GROUP BY purpose",
                                  (since,)).fetchall())
    finally:
        conn.close()
    total = sum(spend.values())
    cap = float(cfg["ai"].get("daily_budget_usd", 0) or 0)
    lines += ["", f"Claude: ${total:.2f} in 24 h" + (" (" + ", ".join(f"{k or 'other'} ${v:.2f}" for k, v in
                                                                         sorted(spend.items(), key=lambda kv: -kv[1])) + ")"
                                                    if spend else "") + (f" · cap ${cap:.0f}/day" if cap else "")]

    # takedowns
    tds = [dict(r) for r in db.query("SELECT t.*, p.clip_id FROM takedowns t LEFT JOIN posts p ON p.id = t.post_id "
                                     "WHERE t.detected_at > ?", (since,))]
    pauses = [dict(r) for r in db.query("SELECT * FROM paused_accounts")]
    lines.append("Takedowns: " + ("; ".join(f"{name(t['channel'])} {_p(t['platform'])} clip #{t['clip_id']} {t['kind']}" for t in tds)
                                  if tds else "none")
                 + " · Paused: " + (", ".join(f"{name(p['channel'])} {_p(p['platform'])}" for p in pauses) if pauses else "none"))

    # waiting for you
    waiting = [dict(r) for r in db.query(
        "SELECT c.id, c.channel, c.quality, c.hook FROM clips c JOIN review_messages m ON m.clip_id = c.id "
        "AND m.kind = 'review' WHERE c.status = 'ready' "
        f"ORDER BY {config_mod.priority_order(cfg, 'c.channel')}c.quality DESC, c.score DESC")] \
        if db.one("SELECT name FROM sqlite_master WHERE name = 'review_messages'") else []
    if waiting:
        lines += ["", f"Waiting for you ({len(waiting)}), best first:"]
        lines += [f"#{w['id']} {name(w['channel'])} q{w['quality']}: {(w['hook'] or '').strip()[:60]}" for w in waiting[:3]]
    url = cfg.get("review_bot", {}).get("dashboard_url")
    if url:
        lines.append(f"Dashboard: {url}")
    return "\n".join(lines)[:4000], snapshot


def due(db: DB, cfg: dict, now: float | None = None) -> bool:
    """Past today's send hour (local time) and today's digest not sent yet."""
    db.conn.executescript(SCHEMA)
    now = now or time.time()
    local = datetime.fromtimestamp(now, _tz(cfg))
    if local.hour < int(cfg.get("digest", {}).get("hour", 9)):
        return False
    return not db.one("SELECT day FROM digest_log WHERE day = ?", (local.strftime("%Y-%m-%d"),))


def send(db: DB, cfg: dict, now: float | None = None, sender=None) -> bool:
    from . import notify

    now = now or time.time()
    text, snapshot = build(db, cfg, now)
    ok = (sender or notify.send)(text)
    if ok:
        day = datetime.fromtimestamp(now, _tz(cfg)).strftime("%Y-%m-%d")
        db.execute("INSERT OR REPLACE INTO digest_log (day, sent_at, views_json) VALUES (?, ?, ?)",
                   (day, now, json.dumps(snapshot)))
    return ok


class DigestSender(threading.Thread):
    def __init__(self, db: DB, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name="digest")
        self.db, self.cfg, self.stop_event = db, cfg, stop

    def run(self) -> None:
        self.stop_event.wait(90)  # let stats refresh after a restart first
        while not self.stop_event.is_set():
            try:
                if due(self.db, self.cfg):
                    log.info("morning digest sent: %s", send(self.db, self.cfg))
            except Exception:
                log.exception("digest failed")
            self.stop_event.wait(300)
