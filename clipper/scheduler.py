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
    """One pass: for each platform with a free slot, post the best eligible clip."""
    from .publishers import get_publisher
    from .publishers.base import PostMeta

    log_lines = []
    platforms = cfg["posting"]["platforms"]
    # human approvals first, then the clips expected to do best
    approved = [_clip_dict(r) for r in db.query(
        "SELECT * FROM clips WHERE status = 'approved' ORDER BY (decided_by = 'human') DESC, "
        "expected_views DESC, quality DESC, score DESC, id ASC")]
    veto = float(cfg.get("auto_approve", {}).get("veto_minutes", 0)) * 60
    for platform in platforms:
        ok, why = guardrails.pacing_ok(cfg, db, platform)
        if not ok:
            log_lines.append(why)
            continue
        pub = get_publisher(platform, cfg)
        ready, msg = pub.ready()
        if not ready:
            log_lines.append(f"{platform}: not configured ({msg.splitlines()[0] if msg else ''})")
            continue
        for clip in approved:
            if db.one("SELECT id FROM posts WHERE clip_id = ? AND platform = ?", (clip["id"], platform)):
                continue
            if clip.get("decided_by") == "auto" and time.time() - (clip.get("decided_at") or 0) < veto:
                continue  # still inside the undo window
            ch = config_mod.channel(cfg, clip["channel"])
            allowed, reason = guardrails.can_post(cfg, ch, clip, platform)
            if not allowed:
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
                "posts", clip_id=clip["id"], platform=platform,
                campaign_id=clip["campaign_id"] or "", remote_id=result.remote_id,
                url=result.url, status=result.status if result.ok else "error",
                error=result.error[:500], posted_at=time.time(),
            )
            line = (f"{platform}: clip {clip['id']} -> {result.status} {result.url}"
                    if result.ok else f"{platform}: clip {clip['id']} failed: {result.error}")
            log.info(line)
            log_lines.append(line)
            break  # one post per platform per pass keeps the pacing honest
    _mark_fully_posted(db, cfg)
    return log_lines


def _mark_fully_posted(db: DB, cfg: dict) -> None:
    platforms = cfg["posting"]["platforms"]
    for clip in db.query("SELECT * FROM clips WHERE status = 'approved'"):
        ch = config_mod.channel(cfg, clip["channel"])
        targets = [p for p in platforms
                   if guardrails.can_post(cfg, ch, _clip_dict(clip), p)[0]]
        done = {r["platform"] for r in db.query(
            "SELECT platform FROM posts WHERE clip_id = ? AND status != 'error'", (clip["id"],))}
        if targets and set(targets) <= done:
            db.update("clips", clip["id"], status="posted")


def refresh_stats(db: DB, cfg: dict) -> int:
    from .publishers import get_publisher

    n = 0
    for post in db.query("SELECT * FROM posts WHERE status = 'published' AND remote_id != ''"):
        pub = get_publisher(post["platform"], cfg)
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
