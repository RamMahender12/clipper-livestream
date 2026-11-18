"""Auto-approval: post the clips this page's own track record says will do well.

Two opinions are combined:

1. A performance model fit on your past posts. Each mature post's views are
   ranked against the other posts on the same platform (a percentile, so
   YouTube and Instagram numbers are comparable). A new clip's expected
   percentile comes from how past clips did with the same streamer, the same
   AI quality score and a similar chat-spike strength, each shrunk toward
   "average" until there's enough data to trust it.
2. A Claude review that sees the clip, the campaign brief and your best and
   worst past posts, and votes approve / hold / reject.

A clip is auto-approved only when both agree. Hard gates that no setting
lifts: test footage, spoiler or unsafe flags, missing AI spoiler check, no
permission. Auto-approved clips wait `veto_minutes` before posting so you can
undo them in the review UI.
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import config as config_mod, guardrails
from .ai import call_json
from .db import DB

log = logging.getLogger(__name__)

PRIOR = 0.5  # an unknown clip is assumed average
PRIOR_WEIGHT = 5  # a factor needs ~5 past clips before it moves the estimate halfway


@dataclass
class PastClip:
    clip_id: int
    channel: str
    quality: int
    score: float
    live: bool  # cut from a live chat spike (vs loudness or a manual cut)
    pct: float  # 0..1, mean platform percentile of its posts
    views: int
    hook: str
    why: str
    duration: float


@dataclass
class Estimate:
    pct: float
    n: int
    parts: dict = field(default_factory=dict)  # factor -> (shrunk mean, sample size)
    expected_views: int = 0

    def describe(self) -> str:
        bits = [f"{k} p{round(m * 100)} (n={n})" for k, (m, n) in self.parts.items()]
        return ", ".join(bits)


@dataclass
class Decision:
    action: str  # approve | hold | reject
    note: str


# ---------------------------------------------------------------- history
def percentile_ranks(values: list[float]) -> list[float]:
    """Mid-rank percentile of each value within the list (ties share a rank)."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    ordered = sorted(values)
    out = []
    for v in values:
        less = sum(1 for o in ordered if o < v)
        equal = sum(1 for o in ordered if o == v)
        out.append((less + 0.5 * equal) / n)
    return out


def load_history(db: DB, cfg: dict, now: float | None = None) -> tuple[list[PastClip], dict[str, list[int]]]:
    """Mature posts with known views, as one PastClip per clip, plus sorted
    view counts per platform (to turn a percentile back into views)."""
    now = now or time.time()
    mature_before = now - float(cfg["auto_approve"]["mature_hours"]) * 3600
    rows = db.query(
        """SELECT p.clip_id, p.platform, p.views, c.channel, c.quality, c.score, c.spike_id,
                  c.hook, c.ai_notes, c.duration_s
           FROM posts p JOIN clips c ON c.id = p.clip_id
           WHERE p.status != 'error' AND p.posted_at IS NOT NULL AND p.posted_at < ?
             AND (p.stats_at IS NOT NULL OR p.views > 0)""",
        (mature_before,),
    )
    by_platform: dict[str, list] = defaultdict(list)
    for r in rows:
        by_platform[r["platform"]].append(r)
    platform_views = {pf: sorted(int(r["views"] or 0) for r in rs) for pf, rs in by_platform.items()}
    pcts: dict[int, list[float]] = defaultdict(list)
    views: dict[int, int] = defaultdict(int)
    info: dict[int, object] = {}
    for rs in by_platform.values():
        for r, pr in zip(rs, percentile_ranks([int(r["views"] or 0) for r in rs])):
            pcts[r["clip_id"]].append(pr)
            views[r["clip_id"]] += int(r["views"] or 0)
            info[r["clip_id"]] = r
    history = []
    for cid, r in info.items():
        try:
            why = json.loads(r["ai_notes"] or "{}").get("why", "")
        except json.JSONDecodeError:
            why = ""
        history.append(PastClip(
            clip_id=cid, channel=r["channel"], quality=int(r["quality"] or 0),
            score=float(r["score"] or 0), live=r["spike_id"] is not None,
            pct=statistics.fmean(pcts[cid]), views=views[cid], hook=r["hook"] or "",
            why=why, duration=float(r["duration_s"] or 0),
        ))
    return history, platform_views


def quality_bucket(q: int) -> str:
    return "low" if q <= 4 else "mid" if q <= 6 else "good" if q <= 8 else "great"


def _shrunk(values: list[float]) -> tuple[float, int]:
    return (sum(values) + PRIOR * PRIOR_WEIGHT) / (len(values) + PRIOR_WEIGHT), len(values)


def _quantile(sorted_vals: list[int], q: float) -> int:
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, max(0, int(q * len(sorted_vals))))
    return int(sorted_vals[idx])


def estimate(
    history: list[PastClip],
    clip: dict,
    platform_views: dict[str, list[int]],
    platforms: list[str] | None = None,
) -> Estimate:
    parts: dict[str, tuple[float, int]] = {}
    parts["streamer"] = _shrunk([h.pct for h in history if h.channel == clip["channel"]])
    qb = quality_bucket(int(clip.get("quality") or 0))
    parts["ai_quality"] = _shrunk([h.pct for h in history if quality_bucket(h.quality) == qb])
    live = clip.get("spike_id") is not None
    same_kind = [h for h in history if h.live == live]
    if len(same_kind) >= 6:
        scores = sorted(h.score for h in same_kind)
        lo, hi = scores[len(scores) // 3], scores[2 * len(scores) // 3]

        def band(s: float) -> int:
            return 0 if s < lo else 1 if s < hi else 2

        b = band(float(clip.get("score") or 0))
        parts["chat_spike" if live else "loudness"] = _shrunk(
            [h.pct for h in same_kind if band(h.score) == b])
    pct = PRIOR + sum(m - PRIOR for m, _ in parts.values())
    pct = min(0.98, max(0.02, pct))
    targets = [p for p in (platforms or platform_views.keys()) if p in platform_views]
    expected = sum(_quantile(platform_views[p], pct) for p in targets)
    return Estimate(pct=pct, n=len(history), parts=parts, expected_views=expected)


# ---------------------------------------------------------------- Claude judge
JUDGE_SYSTEM = """You are the editor of a GTA 6 short-video page during the game's launch weeks. Clips are cut automatically from live streams; you decide which ones get posted to YouTube Shorts, Instagram Reels and similar. Weak posts cost the page reach (platforms show low-retention accounts to fewer people), and paid campaign clips that ignore the brief get rejected and earn nothing, so be selective.

You get the candidate clip (hook, title, caption, transcript, what chat reacted to, length), the campaign brief if it's a paid clip, how this page's past posts performed (best and worst examples with view percentiles), and a statistical estimate from that history.

Decide:
- approve: likely to beat this page's typical post, follows the brief, and the packaging is accurate.
- hold: unclear, so a human should look (borderline moment, packaging needs a fix, or too little evidence).
- reject: boring, confusing without context, misleading packaging, breaks the brief, or anything that could hurt the account.

Judge the moment itself from the transcript and chat's reaction. Use the past examples to learn what this page's audience responds to; when history is thin, rely on your own sense of what makes people stop scrolling. Never approve a clip that spoils GTA 6's story, contains slurs or harassment, or whose hook promises something the clip doesn't show.

confidence is 1-10. predicted_percentile is 0-100: where you expect this clip to land among the page's past posts."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "hold", "reject"]},
        "confidence": {"type": "integer"},
        "predicted_percentile": {"type": "integer"},
        "brief_ok": {"type": "boolean"},
        "issues": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["decision", "confidence", "predicted_percentile", "brief_ok", "issues", "reason"],
    "additionalProperties": False,
}


def _examples(history: list[PastClip], k: int) -> str:
    if not history:
        return "No posts with view data yet."
    ranked = sorted(history, key=lambda h: h.pct, reverse=True)
    top, bottom = ranked[:k], ranked[-k:] if len(ranked) > k else []

    def fmt(h: PastClip) -> str:
        return (f"- p{round(h.pct * 100)} | {h.views:,} views | {h.channel} | quality {h.quality} | "
                f"{h.duration:.0f}s | hook: {h.hook!r} | moment: {h.why}")

    out = ["Best performers:", *map(fmt, top)]
    if bottom:
        out += ["Worst performers:", *map(fmt, bottom)]
    return "\n".join(out)


def _posted_caption(clip: dict) -> str:
    """The caption the platforms will actually get: caption plus stored hashtags."""
    from .publishers.base import PostMeta, caption_with_tags

    tags = [t for t in (clip.get("hashtags") or "").split(",") if t]
    return caption_with_tags(PostMeta(title=clip.get("title") or "", caption=clip.get("caption") or "",
                                      hashtags=tags))


def judge_clip(cfg: dict, clip: dict, notes: dict, ch: dict | None, camp: dict | None,
               history: list[PastClip], est: Estimate) -> tuple[dict | None, str]:
    acfg = cfg["auto_approve"]
    spike_terms = ""
    if clip.get("spike_terms"):
        spike_terms = clip["spike_terms"]
    brief = "unpaid clip (no campaign)"
    if camp:
        brief = json.dumps({k: camp.get(k) for k in ("name", "platforms", "required_tags",
                                                     "disclosure", "min_views", "notes")})
    user = (
        f"Candidate clip #{clip['id']} from {(ch or {}).get('display_name') or clip['channel']}, "
        f"{float(clip.get('duration_s') or 0):.0f}s\n"
        f"Hook (on screen, first 3s): {clip.get('hook')!r}\n"
        f"Title: {clip.get('title')!r}\n"
        f"Caption exactly as it will be posted (hashtags included): {_posted_caption(clip)!r}\n"
        f"What the packaging AI said the moment is: {notes.get('why', '')}\n"
        f"Packaging AI quality score: {clip.get('quality')}/10; spoiler risk: {clip.get('spoiler_risk')}\n"
        f"Chat reaction: {spike_terms or 'n/a'} (spike score {float(clip.get('score') or 0):.1f})\n"
        f"Campaign brief: {brief}\n"
        f"Transcript:\n<transcript>\n{(clip.get('transcript') or '').strip() or '(no speech)'}\n</transcript>\n\n"
        f"This page's history ({est.n} posts with mature view data):\n"
        f"{_examples(history, int(acfg.get('examples', 6)))}\n\n"
        f"Statistical estimate for this clip: p{round(est.pct * 100)}"
        f"{' (' + est.describe() + ')' if est.parts else ''}"
        f"{f', about {est.expected_views:,} views' if est.expected_views else ''}"
    )
    return call_json(cfg, JUDGE_SYSTEM, user, JUDGE_SCHEMA, effort=acfg.get("effort", "high"))


# ---------------------------------------------------------------- decision
def decide(acfg: dict, gate_ok: bool, gate_reason: str, judge: dict | None, judge_err: str,
           est: Estimate, quality: int, approved_today: int) -> Decision:
    if not gate_ok:
        return Decision("hold", f"Needs a human: {gate_reason}")
    if judge is None:
        return Decision("hold", f"AI review unavailable ({judge_err})")
    cold = est.n < int(acfg["min_history"])
    conf = int(judge.get("confidence") or 0)
    stats = (f"cold start: {est.n}/{acfg['min_history']} posts with views" if cold else
             f"history says p{round(est.pct * 100)}"
             f"{f' (~{est.expected_views:,} views)' if est.expected_views else ''}")
    issues = f" Brief issues: {judge['issues']}." if judge.get("issues") and not judge.get("brief_ok", True) else ""
    base = f"AI {judge['decision']} {conf}/10: {judge.get('reason', '')}{issues} | {stats}"

    if judge["decision"] == "approve":
        if not judge.get("brief_ok", True):
            return Decision("hold", f"{base} | held: brief not met")
        if conf < int(acfg["min_confidence"]):
            return Decision("hold", f"{base} | held: AI confidence below {acfg['min_confidence']}")
        if cold:
            if quality < int(acfg["cold_start_min_quality"]):
                return Decision("hold", f"{base} | held: cold start needs quality >= {acfg['cold_start_min_quality']}")
            cap = int(acfg["cold_start_max_per_day"])
        else:
            if est.pct < float(acfg["min_percentile"]):
                return Decision("hold", f"{base} | held: history predicts below p{round(float(acfg['min_percentile']) * 100)}")
            cap = int(acfg["max_per_day"])
        if approved_today >= cap:
            return Decision("hold", f"{base} | held: daily auto-approve limit {cap} reached")
        return Decision("approve", base)
    if judge["decision"] == "reject" and acfg.get("auto_reject", True):
        if cold or est.pct < float(acfg["min_percentile"]):
            return Decision("reject", base)
        return Decision("hold", f"{base} | held: AI says reject but history says it may do well")
    return Decision("hold", base)


def approved_in_last_day(db: DB, now: float | None = None) -> int:
    now = now or time.time()
    return db.one(
        "SELECT COUNT(*) AS n FROM clips WHERE decided_by = 'auto' AND status IN ('approved', 'posted') "
        "AND decided_at > ?", (now - 86400,))["n"]


def run_once(db: DB, cfg: dict, dry_run: bool = False) -> list[str]:
    acfg = cfg["auto_approve"]
    now = time.time()
    lines: list[str] = []

    # launch-week clips go stale fast: drop old undecided ones
    expire = float(acfg.get("expire_hours", 0)) * 3600
    if expire and not dry_run:
        stale = db.query(
            "SELECT id FROM clips WHERE status = 'ready' AND COALESCE(decided_by, '') != 'human' "
            "AND created_at < ?", (now - expire,))
        for r in stale:
            db.update("clips", r["id"], status="rejected", decided_by="auto", decided_at=now,
                      decision_note=f"Expired: still unposted after {acfg['expire_hours']}h")
            lines.append(f"clip {r['id']}: expired")

    history, platform_views = load_history(db, cfg, now)
    approved_today = approved_in_last_day(db, now)
    rows = db.query(
        "SELECT c.*, s.top_terms AS spike_terms FROM clips c LEFT JOIN spikes s ON s.id = c.spike_id "
        "WHERE c.status = 'ready' AND COALESCE(c.decided_by, '') != 'human' "
        "AND (c.auto_checked_at IS NULL OR c.auto_checked_at < c.updated_at) "
        "ORDER BY c.score DESC LIMIT ?", (int(acfg.get("batch", 10)),))
    for row in rows:
        clip = dict(row)
        try:
            notes = json.loads(clip.get("ai_notes") or "{}")
        except json.JSONDecodeError:
            notes = {}
        ch = config_mod.channel(cfg, clip["channel"])
        camp = guardrails.campaign_for(cfg, ch)
        gate_ok, gate_reason = guardrails.auto_gate(cfg, ch, clip, notes)
        est = estimate(history, clip, platform_views, cfg["posting"]["platforms"])
        judge, err = judge_clip(cfg, clip, notes, ch, camp, history, est) if gate_ok else (None, "")
        d = decide(acfg, gate_ok, gate_reason, judge, err, est, int(clip.get("quality") or 0), approved_today)
        lines.append(f"clip {clip['id']}: {d.action.upper()} - {d.note}")
        if dry_run:
            continue
        t = time.time()
        fields = dict(auto_checked_at=t, updated_at=t, decision_note=d.note[:1500],
                      expected_views=est.expected_views)
        if d.action == "approve":
            fields.update(status="approved", decided_by="auto", decided_at=t)
            approved_today += 1
        elif d.action == "reject":
            fields.update(status="rejected", decided_by="auto", decided_at=t)
        db.update("clips", clip["id"], **fields)
        log.info("auto-review clip %s: %s", clip["id"], d.action)
    return lines


def waiting_for_human(db: DB) -> list:
    """Ready clips the auto-approver already looked at (and held), or that a
    human took over. They stay put until someone acts or the clip changes."""
    return db.query(
        "SELECT id, channel, decision_note FROM clips WHERE status = 'ready' AND "
        "(COALESCE(decided_by, '') = 'human' OR "
        " (auto_checked_at IS NOT NULL AND auto_checked_at >= updated_at)) "
        "ORDER BY score DESC, id ASC")


def status(db: DB, cfg: dict) -> dict:
    acfg = cfg["auto_approve"]
    history, _ = load_history(db, cfg)
    cold = len(history) < int(acfg["min_history"])
    return {
        "enabled": bool(acfg.get("enabled")),
        "today": approved_in_last_day(db),
        "limit": int(acfg["cold_start_max_per_day"] if cold else acfg["max_per_day"]),
        "history": len(history),
        "min_history": int(acfg["min_history"]),
    }


class AutoApprover(threading.Thread):
    def __init__(self, db: DB, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name="auto-approve")
        self.db, self.cfg, self.stop_event = db, cfg, stop

    def run(self) -> None:
        interval = float(self.cfg["auto_approve"].get("interval_minutes", 3)) * 60
        while not self.stop_event.wait(interval):
            try:
                run_once(self.db, self.cfg)
            except Exception:
                log.exception("auto-approve pass failed")
