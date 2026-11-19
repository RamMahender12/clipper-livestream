"""Posts approved clips through official APIs, paced per platform, and keeps
view counts fresh for the earnings scoreboard."""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import threading
import time
from pathlib import Path

from . import config as config_mod, guardrails
from .db import DB

log = logging.getLogger(__name__)


def _clip_dict(row) -> dict:
    d = dict(row)
    try:
        d["unsafe"] = bool(json.loads(d.get("ai_notes") or "{}").get("unsafe"))
    except json.JSONDecodeError:
        d["unsafe"] = False
    return d


RETRY_MINUTES = [10, 30, 90]  # waits before upload tries 2, 3 and 4 ([posting].retry_minutes)


@contextlib.contextmanager
def _posting_lock(cfg: dict):
    """One posting pass at a time, across processes too: `clipper post` run while the service is
    up would otherwise upload the same clip to the same account twice."""
    path = config_mod.data_dir(cfg) / "posting.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def post_once(db: DB, cfg: dict) -> list[str]:
    """One pass: each account with a free slot posts its best eligible clip.
    Every streamer posts to its own accounts; a pass posts at most one clip
    per account, then sends one Telegram alert listing what to submit."""
    with _posting_lock(cfg) as mine:
        if not mine:
            return ["another posting pass is running (the background service posts on its own): nothing done"]
        return _post_once(db, cfg)


def _retry_note(cfg: dict, retry_at: float | None, safe: bool, attempts: int) -> str:
    if retry_at:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(cfg["posting"].get("timezone", "UTC"))
        return f"trying again at {datetime.fromtimestamp(retry_at, tz):%H:%M}"
    return f"gave up after {attempts} tries" if safe else "not retried automatically"


def _post_once(db: DB, cfg: dict) -> list[str]:
    from .publishers import channel_account, get_publisher
    from .publishers.base import PostMeta

    log_lines: list[str] = []
    posted: list[dict] = []
    failed: list[str] = []
    platforms = cfg["posting"]["platforms"]
    delays = [float(m) for m in cfg["posting"].get("retry_minutes", RETRY_MINUTES)]
    # human approvals first, then the clips expected to do best
    approved = [_clip_dict(r) for r in db.query(
        "SELECT * FROM clips WHERE status = 'approved' ORDER BY (decided_by = 'human') DESC, "
        "expected_views DESC, quality DESC, score DESC, id ASC")]
    veto = float(cfg.get("auto_approve", {}).get("veto_minutes", 0)) * 60
    for platform in platforms:
        used: set[str] = set()       # accounts that already posted this pass
        blocked: dict[str, str] = {}  # account -> why it can't post now
        for clip in approved:
            prev = db.one("SELECT id, status, attempts, retry_at FROM posts WHERE clip_id = ? AND platform = ?",
                          (clip["id"], platform))
            # already there, or a failure that isn't safe (or not yet time) to try again
            if prev and not (prev["status"] == "error" and prev["retry_at"] and prev["retry_at"] <= time.time()):
                continue
            if clip.get("decided_by") == "auto" and time.time() - (clip.get("decided_at") or 0) < veto:
                continue  # still inside the undo window
            ch = config_mod.channel(cfg, clip["channel"])
            allowed, _ = guardrails.can_post(cfg, ch, clip, platform)
            if not allowed:
                continue
            account = channel_account(cfg, ch, platform)
            if not account or account in used or account in blocked:
                continue
            from . import takedowns

            if why := takedowns.paused(db, account):
                blocked[account] = f"{platform}/{clip['channel']}: paused after a takedown ({why[:80]})"
                continue
            ok, why = guardrails.pacing_ok(cfg, db, platform, account=account)
            if not ok:
                blocked[account] = why
                continue
            pub = get_publisher(platform, cfg, ch)
            ready, msg = pub.ready()
            if not ready:
                blocked[account] = f"{platform}/{clip['channel']}: not configured ({(msg or '').splitlines()[0] if msg else ''})"
                continue
            camp = guardrails.campaign_for(cfg, ch)
            meta = PostMeta(
                title=clip["title"] or f"GTA 6 clip #{clip['id']}",
                caption=clip["caption"],
                hashtags=[t for t in (clip["hashtags"] or "").split(",") if t],
                is_paid_promotion=bool(camp and camp.get("paid_promotion", True)),
                clip_id=clip["id"],
            )
            # uploads earlier in this pass take minutes: you may have undone or rejected it meanwhile
            now_row = db.one("SELECT status, decided_by, decided_at FROM clips WHERE id = ?", (clip["id"],))
            if not now_row or now_row["status"] != "approved" or (
                    now_row["decided_by"] == "auto" and time.time() - (now_row["decided_at"] or 0) < veto):
                continue
            result = pub.publish(Path(clip["final_path"]), meta)
            # the channel's current campaign: a clip cut before joining still counts
            campaign_id = (camp or {}).get("id", "") or clip["campaign_id"] or ""
            if campaign_id and campaign_id != clip["campaign_id"]:
                db.update("clips", clip["id"], campaign_id=campaign_id)
            attempts = int(prev["attempts"] or 1) + 1 if prev else 1
            safe = not result.ok and result.retry
            retry_at = time.time() + delays[attempts - 1] * 60 if safe and attempts <= len(delays) else None
            fields = dict(account=account, campaign_id=campaign_id, remote_id=result.remote_id,
                          url=result.url, status=result.status if result.ok else "error",
                          error=result.error[:500], posted_at=time.time(), attempts=attempts, retry_at=retry_at)
            if prev:
                db.update("posts", prev["id"], **fields)
            else:
                db.insert("posts", clip_id=clip["id"], platform=platform, **fields)
            used.add(account)
            if result.ok:
                line = f"{platform}/{clip['channel']}: clip {clip['id']} -> {result.status} {result.url}"
                if attempts > 1:
                    line += f" (try {attempts})"
                if platform != "export" and result.status == "published":
                    posted.append({"campaign": camp, "platform": platform, "url": result.url,
                                   "clip_id": clip["id"], "hook": clip.get("hook", "")})
            else:
                note = _retry_note(cfg, retry_at, safe, attempts)
                line = f"{platform}/{clip['channel']}: clip {clip['id']} failed ({note}): {result.error}"
                failed.append(f"{platform} @ {clip['channel']}, clip {clip['id']} ({note}): {result.error[:200]}")
            log.info(line)
            log_lines.append(line)
        log_lines.extend(blocked.values())
    _mark_fully_posted(db, cfg)
    if posted or failed:
        from . import submit_tracker

        submit_tracker.send_batch(cfg, db, posted, failed)  # links + a ✅ Submitted button per campaign
    return log_lines


def _mark_fully_posted(db: DB, cfg: dict) -> None:
    """A clip is done once it's on every platform its channel has an account for."""
    from .publishers import channel_account

    platforms = cfg["posting"]["platforms"]
    for clip in db.query("SELECT * FROM clips WHERE status = 'approved'"):
        ch = config_mod.channel(cfg, clip["channel"])
        targets = [p for p in platforms
                   if channel_account(cfg, ch, p) and guardrails.can_post(cfg, ch, _clip_dict(clip), p)[0]]
        done = {r["platform"] for r in db.query(
            "SELECT platform FROM posts WHERE clip_id = ? AND status != 'error'", (clip["id"],))}
        if targets and set(targets) <= done:
            db.update("clips", clip["id"], status="posted")


def refresh_stats(db: DB, cfg: dict) -> int:
    from .publishers import get_publisher

    from .publishers.base import stats_many

    # one batch per account: YouTube reads 50 videos per quota unit instead of one
    groups: dict[tuple[str, str], list] = {}
    for post in db.query("SELECT p.*, c.channel FROM posts p JOIN clips c ON c.id = p.clip_id "
                         "WHERE p.status = 'published' AND p.remote_id != ''"):
        groups.setdefault((post["platform"], post["channel"]), []).append(post)
    n = 0
    for (platform, channel), posts in groups.items():
        pub = get_publisher(platform, cfg, config_mod.channel(cfg, channel))
        try:
            found = stats_many(pub, [p["remote_id"] for p in posts])
        except Exception as exc:
            log.warning("stats %s/%s failed: %s", platform, channel, exc)
            continue
        for post in posts:
            if stats := found.get(post["remote_id"]):
                db.update("posts", post["id"], views=stats.views, likes=stats.likes,
                          comments=stats.comments, stats_at=time.time())
                from . import stats as stats_mod

                stats_mod.record_views(db, post["id"], stats.views)  # view history for the /stats charts
                n += 1
    return n


def earnings_summary(db: DB, cfg: dict) -> dict:
    rows = db.query("SELECT * FROM posts WHERE status != 'error'")
    by_campaign: dict[str, dict] = {}
    total_views, total_est = 0, 0.0
    for p in rows:
        camp = config_mod.campaign(cfg, p["campaign_id"])
        est = guardrails.estimated_earnings(p["views"] or 0, camp)
        key = p["campaign_id"] or "(unpaid)"
        agg = by_campaign.setdefault(key, {"posts": 0, "views": 0, "est_usd": 0.0})
        agg["posts"] += 1
        agg["views"] += p["views"] or 0
        agg["est_usd"] = round(agg["est_usd"] + est, 2)
        total_views += p["views"] or 0
        total_est += est
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM clips GROUP BY status")}
    by_decider: dict[str, dict] = {}
    for r in db.query(
        "SELECT COALESCE(NULLIF(c.decided_by, ''), 'human') AS who, COUNT(DISTINCT c.id) AS clips, "
        "COALESCE(SUM(p.views), 0) AS views FROM clips c JOIN posts p ON p.clip_id = c.id "
        "WHERE p.status != 'error' GROUP BY who"):
        by_decider[r["who"]] = {"clips": r["clips"], "views": r["views"],
                                "views_per_clip": round(r["views"] / r["clips"]) if r["clips"] else 0}
    return {"clips": counts, "posts": len(rows), "views": total_views,
            "est_usd": round(total_est, 2), "by_campaign": by_campaign, "by_decider": by_decider}


class Scheduler(threading.Thread):
    def __init__(self, db: DB, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name="scheduler")
        self.db, self.cfg, self.stop_event = db, cfg, stop
        self._last_stats = 0.0

    def run(self) -> None:
        refresh = float(self.cfg["stats"]["refresh_minutes"]) * 60
        while not self.stop_event.wait(60):
            try:
                post_once(self.db, self.cfg)
                if time.time() - self._last_stats > refresh:
                    refresh_stats(self.db, self.cfg)
                    self._last_stats = time.time()
            except Exception:
                log.exception("scheduler pass failed")
