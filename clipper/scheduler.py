"""Posts approved clips through official APIs, paced per platform, and keeps
view counts fresh for the earnings scoreboard."""
from __future__ import annotations

import contextlib
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

from . import config as config_mod, guardrails
from .db import DB
from .metadata import required_tags, with_question


def with_required_tags(ch: dict, camp: dict | None, stored: str | None) -> list[str]:
    """Stored hashtags, with the brief's required ones put back first if a review edit dropped them."""
    tags = [t.strip().lstrip("#") for t in (stored or "").split(",") if t.strip()]
    have = {t.lower() for t in tags}
    return [t for t in required_tags(ch, camp) if t.lower() not in have] + tags

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
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if sys.platform == "win32":
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)


def post_once(db: DB, cfg: dict) -> list[str]:
    """One pass: each account with a free slot posts its best eligible clip.
    Every streamer posts to its own accounts; a pass posts at most one clip
    per account, then sends one Telegram alert listing what to submit."""
    with _posting_lock(cfg) as mine:
        if not mine:
            return ["another posting pass is running (the background service posts on its own): nothing done"]
        return _post_once(db, cfg)


def _approved(db: DB, cfg: dict) -> list[dict]:
    """Approved clips in posting order: [posting].priority streamers first, then human approvals,
    then the clips expected to do best."""
    return [_clip_dict(r) for r in db.query(
        f"SELECT * FROM clips WHERE status = 'approved' ORDER BY {config_mod.priority_order(cfg)}"
        "(decided_by = 'human') DESC, expected_views DESC, quality DESC, score DESC, id ASC")]


def _slots(db: DB, cfg: dict, platform: str, account: str, now: float) -> Iterator[tuple[float, str]]:
    """(time, why it's then) for each upcoming post of this account, in order: min gap after the
    last post and no more than the daily cap in any 24h, the same rules as guardrails.pacing_ok.
    ponytail: ignores upload failures; add if those get used."""
    posting = cfg["posting"]
    cap = int(posting.get("max_posts_per_day", {}).get(platform, 10))
    gap_min = float(posting.get("min_gap_minutes", 45))
    # export has no gap but still posts one clip per pass, and a pass runs every 60 s
    gap = 60.0 if platform == "export" else gap_min * 60
    times = [r["posted_at"] for r in db.query(
        "SELECT posted_at FROM posts WHERE platform = ? AND account = ? AND status != 'error' "
        "AND posted_at > ? ORDER BY posted_at", (platform, account, now - 86400))]
    t = now
    while True:
        why = ""
        if times and times[-1] + gap > t:
            t = times[-1] + gap
            why = "" if platform == "export" else f"{gap_min:g}-min gap between posts"
        window = [x for x in times if x > t - 86400]
        if len(window) >= cap:
            t = window[len(window) - cap] + 86400  # when enough of the last day's posts age out
            why = f"daily limit reached ({cap} uploads per 24h)"
        if platform != "export" and (later := _after_quiet(cfg, t)) != t:
            t, why = later, why or "outside posting hours"
        times.append(t)
        yield t, why


def _after_quiet(cfg: dict, t: float) -> float:
    """The first moment at or after t that isn't in [posting].quiet_hours."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    quiet = set(cfg["posting"].get("quiet_hours", []))
    tz = ZoneInfo(cfg["posting"].get("timezone", "UTC"))
    for _ in range(25):
        dt = datetime.fromtimestamp(t, tz)
        if dt.hour not in quiet:
            return t
        t = dt.replace(minute=0, second=0, microsecond=0).timestamp() + 3600
    return t


def _dur(sec: float) -> str:
    m = int(sec / 60) + 1
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m} min"


def _clock(cfg: dict, ts: float) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime.fromtimestamp(ts, ZoneInfo(cfg["posting"].get("timezone", "UTC")))
    days = (dt.date() - datetime.now(dt.tzinfo).date()).days
    return f"{dt:%H:%M}" + ("" if days == 0 else " tomorrow" if days == 1 else f" {dt:%a}")


def waiting_reasons(db: DB, cfg: dict) -> dict[int, list[str]]:
    """Why each approved clip hasn't posted yet, per platform, for the review cards.
    Read-only: the same checks as _post_once, plus the clip's place in its account's line."""
    from . import takedowns
    from .publishers import channel_account

    now = time.time()
    veto = float(cfg.get("auto_approve", {}).get("veto_minutes", 0)) * 60
    slots: dict[tuple[str, str], Iterator[tuple[float, str]]] = {}  # (platform, account) -> upcoming posts
    ahead: dict[tuple[str, str], int] = {}                          # clips earlier in line for that account
    out: dict[int, list[str]] = {}
    for clip in _approved(db, cfg):
        lines = out.setdefault(clip["id"], [])
        ch = config_mod.channel(cfg, clip["channel"])
        left = veto - (now - (clip.get("decided_at") or 0)) if clip.get("decided_by") == "auto" else 0
        for platform in cfg["posting"]["platforms"]:
            name = PLATFORM_NAMES.get(platform, platform)
            prev = db.one("SELECT status, retry_at, error FROM posts WHERE clip_id = ? AND platform = ?",
                          (clip["id"], platform))
            if prev and prev["status"] != "error":
                continue  # already there
            if prev and not prev["retry_at"]:
                continue  # gave up: the error shows in the Posts row
            account = channel_account(cfg, ch, platform)
            allowed, why = guardrails.can_post(cfg, ch, clip, platform)
            if not account or not allowed:
                if account:
                    lines.append(f"{name}: won't upload ({why})")
                continue
            if paused := takedowns.paused(db, account):
                lines.append(f"{name}: on hold, account paused after a takedown ({paused[:60]})")
                continue
            key = (platform, account)
            if key not in slots:
                slots[key] = _slots(db, cfg, platform, account, now)
            n = ahead.get(key, 0)
            ahead[key] = n + 1
            at, reason = next(slots[key])
            reasons = [reason] if reason else []
            # ponytail: an auto clip still in its undo window really yields its slot to the next clip;
            # pushing it back is off by at most veto_minutes for that one clip
            if now + left > at:
                at, reasons = now + left, [f"{veto / 60:g}-min undo window after AI approval"]
            if prev and prev["retry_at"] > at:  # a refused upload waits for its retry time
                at, reasons = prev["retry_at"], []
            if prev:
                reasons.append(f"retrying, last try failed: {(prev['error'] or '')[:80]}")
            if n:
                reasons.append(f"{n} clip{'s' * (n > 1)} ahead in line")
            when = ("within a minute" if at - now < 90
                    else f"at {_clock(cfg, at)} (in {_dur(at - now)})")
            lines.append(f"{name}: uploads {when}" + (" · " + " · ".join(reasons) if reasons else ""))
    return out


PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok",
                  "export": "Local copy"}


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
    approved = _approved(db, cfg)
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
                caption=with_question(clip["caption"] or "", clip["id"]),
                hashtags=with_required_tags(ch, camp, clip["hashtags"]),
                is_paid_promotion=bool(camp and camp.get("paid_promotion", True)),
                clip_id=clip["id"],
                take=_take(clip),
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
    from .publishers import drive

    try:
        drive.prune_posted(db, cfg)  # the Drive copy was only for review; free the space
    except Exception:
        log.exception("Drive cleanup failed")


def _take(clip: dict) -> str:
    try:
        return str((json.loads(clip.get("ai_notes") or "{}") or {}).get("take") or "")[:200]
    except (json.JSONDecodeError, TypeError):
        return ""


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
