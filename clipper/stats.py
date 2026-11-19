"""Everything the /stats page shows: campaigns, earnings, spend, breakeven, funnel, health.

Money in: per-view campaigns pay rate_per_1k per post once it reaches min_views (capped per clip);
pool campaigns pay nothing until our posts together reach pool_min_views, then a share of the pool
we can't know until the campaign ends.
Money out: Claude calls are metered per streamer (ai_usage.channel); calls from before the meter
tracked streamers, and the [costs] entries (server, unmetered AI), are shared out by each streamer's
share of clips made.
"""
from __future__ import annotations

import json
import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config as config_mod, guardrails
from .db import DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS post_views (
    post_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    views INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS post_views_post ON post_views(post_id, ts);
"""


def record_views(db: DB, post_id: int, views: int, now: float | None = None) -> None:
    """Called whenever a post's stats are refreshed, so the page can chart views over time."""
    db.conn.executescript(SCHEMA)
    db.execute("INSERT INTO post_views (post_id, ts, views) VALUES (?, ?, ?)", (post_id, now or time.time(), int(views)))


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg["posting"].get("timezone", "UTC"))


def _ai_rows(cfg: dict) -> list[tuple]:
    from . import ai

    conn = ai._db(cfg)
    try:
        return conn.execute("SELECT ts, day, purpose, channel, cost_usd FROM ai_usage").fetchall()
    finally:
        conn.close()


def _views_at(db: DB, post_id: int, ts: float, fallback: int) -> int:
    row = db.one("SELECT views FROM post_views WHERE post_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1", (post_id, ts))
    return int(row["views"]) if row else fallback


def build(db: DB, cfg: dict, now: float | None = None) -> dict:
    from . import digest, submit_tracker, takedowns

    db.conn.executescript(SCHEMA)
    db.conn.executescript(submit_tracker.SCHEMA)
    takedowns._schema(db)
    now = now or time.time()
    tz = _tz(cfg)
    costs = cfg.get("costs", {})
    channels = [c for c in cfg.get("channels", []) if guardrails.permission(c)[0] in ("campaign", "written", "own")]
    name = {c["login"]: c.get("display_name") or c["login"] for c in channels}

    posts = [dict(r) for r in db.query(
        "SELECT p.*, c.channel, c.hook, c.title, c.decided_by, c.decision_note FROM posts p "
        "JOIN clips c ON c.id = p.clip_id WHERE p.status = 'published'")]
    for p in posts:
        p["views"] = int(p["views"] or 0)
        p["views_24h_ago"] = 0 if (p["posted_at"] or 0) > now - 86400 else _views_at(db, p["id"], now - 86400, p["views"])
        p["gained_24h"] = max(0, p["views"] - p["views_24h_ago"])
        p["source"] = ("daily pick" if (p["decision_note"] or "").startswith("Daily pick")
                       else "you" if p["decided_by"] == "human" else "AI judge")

    # clips made per streamer (all time), used to share out costs we can't pin on one streamer
    made = {r["channel"]: r["n"] for r in db.query("SELECT channel, COUNT(*) n FROM clips GROUP BY channel")}
    made_active = {c["login"]: made.get(c["login"], 0) for c in channels}
    total_made = sum(made_active.values()) or 1
    share = {k: v / total_made for k, v in made_active.items()}

    # spend
    ai_rows = _ai_rows(cfg)
    ai_by_channel = Counter()
    unattributed = 0.0
    by_day = defaultdict(Counter)
    for ts, day, purpose, channel, cost in ai_rows:
        if channel and channel in share:
            ai_by_channel[channel] += cost
        else:
            unattributed += cost
        local_day = datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
        by_day[local_day][purpose or "other"] += cost
    unmetered = float(costs.get("ai_unmetered_usd", 0) or 0)
    started = costs.get("started")
    days_running = max(0.0, (now - datetime.fromisoformat(started).replace(tzinfo=tz).timestamp()) / 86400) if started else 0.0
    server_total = float(costs.get("server_usd_per_day", 0) or 0) * days_running
    spend_by_channel = {}
    for login in share:
        shared = (unattributed + unmetered + server_total) * share[login]
        spend_by_channel[login] = {"ai_metered": round(ai_by_channel[login], 2),
                                   "shared": round(shared, 2),
                                   "total": round(ai_by_channel[login] + shared, 2)}
    ai_total = sum(c for *_, c in ai_rows) + unmetered
    spend_total = ai_total + server_total

    # campaigns
    campaigns = []
    for camp in cfg.get("campaigns", []):
        if not camp.get("active"):
            continue
        logins = [c["login"] for c in channels if guardrails.permission(c) == ("campaign", camp["id"])]
        cposts = [p for p in posts if p.get("campaign_id") == camp["id"] or (not p.get("campaign_id") and p["channel"] in logins)]
        views = sum(p["views"] for p in cposts)
        spend = sum(spend_by_channel.get(l, {}).get("total", 0) for l in logins)
        pool = int(camp.get("pool_min_views") or 0)
        rate = float(camp.get("rate_per_1k") or 0)
        row = {
            "id": camp["id"], "name": camp.get("name", camp["id"]), "streamers": [name.get(l, l) for l in logins],
            "kind": "pool" if pool or not rate else "per view", "url": camp.get("url", ""),
            "posts": len(cposts), "views": views, "gained_24h": sum(p["gained_24h"] for p in cposts),
            "avg_views": round(views / len(cposts)) if cposts else 0,
            "best": max((p["views"] for p in cposts), default=0),
            "by_platform": {pl: sum(p["views"] for p in cposts if p["platform"] == pl) for pl in ("youtube", "instagram")},
            "spend": round(spend, 2),
        }
        if row["kind"] == "per view":
            min_views = int(camp.get("min_views") or 0)
            earned = sum(guardrails.estimated_earnings(p["views"], camp) for p in cposts)
            per_min_clip = min_views / 1000 * rate if min_views else 0
            need = max(0.0, spend - earned)
            row.update(earned=round(earned, 2), rate_per_1k=rate, min_views=min_views, cap=camp.get("cap_per_clip"),
                       paying_posts=sum(1 for p in cposts if p["views"] >= min_views),
                       breakeven={"usd_needed": round(need, 2),
                                  "views_needed": math.ceil(need / rate * 1000) if rate else None,
                                  "clips_at_min": math.ceil(need / per_min_clip) if per_min_clip else None})
        else:
            budget = float(camp.get("budget_usd") or 0) or None
            row.update(earned=0.0, pool_min_views=pool, budget=budget, progress=round(views / pool, 4) if pool else None,
                       views_to_threshold=max(0, pool - views) if pool else None,
                       breakeven={"usd_needed": round(spend, 2),
                                  "pool_share_needed": round(spend / budget, 4) if budget else None,
                                  "note": (f"pays nothing until our posts reach {pool:,} views in total; after that "
                                           "our share of the pool must beat the spend") if pool else "pool share unknown"})
        row["net"] = round(row["earned"] - row["spend"], 2)
        campaigns.append(row)

    # funnel per streamer, last 24 h
    since = now - 86400
    funnel = []
    for c in channels:
        rows = [dict(r) for r in db.query("SELECT status, decided_by, decision_note, final_path, ai_notes FROM clips "
                                          "WHERE channel = ? AND created_at > ?", (c["login"], since))]
        checked = 0
        for r in rows:
            try:
                checked += bool((json.loads(r["ai_notes"] or "{}") or {}).get("gemini"))
            except (json.JSONDecodeError, TypeError):
                pass
        why = Counter(digest._reason(r["decision_note"]) for r in rows
                      if r["status"] in ("ready", "rejected") and r["decided_by"] != "human")
        funnel.append({
            "streamer": name[c["login"]], "made": len(rows),
            "rendered": sum(1 for r in rows if r["final_path"]), "video_checked": checked,
            "approved": sum(1 for r in rows if r["status"] in ("approved", "posted")),
            "picks": sum(1 for r in rows if (r["decision_note"] or "").startswith("Daily pick")),
            "posted": sum(1 for r in rows if r["status"] == "posted"),
            "waiting_for_you": sum(1 for r in rows if r["status"] == "ready" and (r["decision_note"] or "").startswith(("AI hold", "Needs a human"))),
            "top_reasons": why.most_common(4),
        })

    # views over the last 7 days, per platform (end-of-day totals)
    today = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    first = db.one("SELECT MIN(ts) t FROM post_views")["t"]
    series = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        end = min(now, (day + timedelta(days=1)).timestamp())
        point = {"day": day.strftime("%d %b")}
        for pl in ("youtube", "instagram"):
            live = [p for p in posts if p["platform"] == pl and (p["posted_at"] or 0) <= end]
            if end >= now - 60:
                point[pl] = sum(p["views"] for p in live)  # today: current numbers
            elif first is None or end < first:
                point[pl] = None  # before view history was recorded: unknown
            else:
                point[pl] = sum(_views_at(db, p["id"], end, 0) for p in live)
        series.append(point)
    spend_series = []
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        spend_series.append({"day": (today - timedelta(days=i)).strftime("%d %b"),
                             **{k: round(v, 3) for k, v in by_day.get(d, {}).items()}})

    # health
    from . import ai

    q = db.one("SELECT COUNT(*) n, MIN(created_at) oldest FROM clips WHERE status = 'queued'")
    subs = [dict(r) for r in db.query("SELECT * FROM submissions")]
    last_clip = {r["channel"]: r["t"] for r in db.query("SELECT channel, MAX(created_at) t FROM clips GROUP BY channel")}
    platforms = {}
    for pl in ("youtube", "instagram"):
        pp = [p for p in posts if p["platform"] == pl]
        platforms[pl] = {"posts": len(pp), "views": sum(p["views"] for p in pp),
                         "avg": round(sum(p["views"] for p in pp) / len(pp)) if pp else 0,
                         "gained_24h": sum(p["gained_24h"] for p in pp)}
    earned = sum(c["earned"] for c in campaigns)
    return {
        "generated_at": now,
        "overview": {
            "posts": len(posts), "views": sum(p["views"] for p in posts),
            "gained_24h": sum(p["gained_24h"] for p in posts),
            "earned": round(earned, 2), "spend": round(spend_total, 2), "net": round(earned - spend_total, 2),
            "ai_spend": round(ai_total, 2), "server_spend": round(server_total, 2),
            "ai_today": round(ai.spent_today(cfg), 2), "ai_cap": float(cfg["ai"].get("daily_budget_usd", 0) or 0),
            "days_running": round(days_running, 1),
        },
        "campaigns": campaigns,
        "platforms": platforms,
        "funnel": funnel,
        "spend_by_streamer": {name[k]: v for k, v in spend_by_channel.items()},
        "views_series": series,
        "spend_series": spend_series,
        "top_posts": [{k: p[k] for k in ("clip_id", "platform", "url", "views", "gained_24h", "source", "posted_at")}
                      | {"streamer": name.get(p["channel"], p["channel"]), "title": (p["title"] or p["hook"] or "")[:80]}
                      for p in sorted(posts, key=lambda p: p["views"], reverse=True)[:12]],
        "health": {
            "queued": q["n"], "oldest_queued_min": round((now - q["oldest"]) / 60) if q["oldest"] else 0,
            "last_clip_min": {name.get(k, k): round((now - v) / 60) for k, v in last_clip.items() if k in name},
            "submissions": {"total": len(subs),
                            "on_time": sum(1 for s in subs if s["submitted_at"] and s["submitted_at"] <= s["deadline"]),
                            "late": sum(1 for s in subs if s["submitted_at"] and s["submitted_at"] > s["deadline"]),
                            "missed": sum(1 for s in subs if not s["submitted_at"] and s["deadline"] < now),
                            "open": sum(1 for s in subs if not s["submitted_at"] and s["deadline"] >= now)},
            "takedowns": [dict(r) for r in db.query("SELECT channel, platform, kind, detail, url, detected_at FROM takedowns ORDER BY detected_at DESC LIMIT 10")],
            "paused": [dict(r) for r in db.query("SELECT channel, platform, reason FROM paused_accounts")],
        },
        "notes": {
            "costs": ("Server cost is an estimate ([costs].server_usd_per_day) and is paid from AWS credits; "
                      "AI spend before per-streamer metering, and the unmetered launch-day spend, are shared by clips made."),
        },
    }
