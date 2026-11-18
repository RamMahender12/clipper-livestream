"""Posts approved clips through official APIs, paced per platform, and keeps
view counts fresh for the earnings scoreboard."""
from __future__ import annotations

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


def post_once(db: DB, cfg: dict) -> list[str]:
    """One pass: each account with a free slot posts its best eligible clip.
    Every streamer posts to its own accounts; a pass posts at most one clip
    per account, then sends one Telegram alert listing what to submit."""
    from . import notify
    from .publishers import channel_account, get_publisher
    from .publishers.base import PostMeta

    log_lines: list[str] = []
    posted: list[dict] = []
    failed: list[str] = []
    platforms = cfg["posting"]["platforms"]
    # human approvals first, then the clips expected to do best
    approved = [_clip_dict(r) for r in db.query(
        "SELECT * FROM clips WHERE status = 'approved' ORDER BY (decided_by = 'human') DESC, "
        "expected_views DESC, quality DESC, score DESC, id ASC")]
    veto = float(cfg.get("auto_approve", {}).get("veto_minutes", 0)) * 60
    for platform in platforms:
        used: set[str] = set()       # accounts that already posted this pass
        blocked: dict[str, str] = {}  # account -> why it can't post now
        for clip in approved:
            if db.one("SELECT id FROM posts WHERE clip_id = ? AND platform = ?", (clip["id"], platform)):
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
            result = pub.publish(Path(clip["final_path"]), meta)
            db.insert(
                "posts", clip_id=clip["id"], platform=platform, account=account,
                campaign_id=clip["campaign_id"] or "", remote_id=result.remote_id,
                url=result.url, status=result.status if result.ok else "error",
                error=result.error[:500], posted_at=time.time(),
            )
            used.add(account)
            if result.ok:
                line = f"{platform}/{clip['channel']}: clip {clip['id']} -> {result.status} {result.url}"
                if platform != "export" and result.status == "published":
                    posted.append({"campaign": camp, "platform": platform, "url": result.url,
                                   "clip_id": clip["id"], "hook": clip.get("hook", "")})
            else:
                line = f"{platform}/{clip['channel']}: clip {clip['id']} failed: {result.error}"
                failed.append(f"{platform} @ {clip['channel']}, clip {clip['id']}: {result.error[:200]}")
            log.info(line)
            log_lines.append(line)
        log_lines.extend(blocked.values())
    _mark_fully_posted(db, cfg)
    if posted or failed:
        notify.send(notify.batch_message(cfg, posted, failed))
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

    n = 0
    for post in db.query("SELECT p.*, c.channel FROM posts p JOIN clips c ON c.id = p.clip_id "
                         "WHERE p.status = 'published' AND p.remote_id != ''"):
        ch = config_mod.channel(cfg, post["channel"])
        pub = get_publisher(post["platform"], cfg, ch)
        try:
            stats = pub.fetch_stats(post["remote_id"])
        except Exception as exc:
            log.warning("stats %s/%s failed: %s", post["platform"], post["remote_id"], exc)
            continue
        if stats:
            db.update("posts", post["id"], views=stats.views, likes=stats.likes,
                      comments=stats.comments, stats_at=time.time())
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
