"""Rules that keep the operation legal and the accounts alive.

1. Permission: a channel is only recorded if config says why we're allowed to
   (paid campaign, written permission, our own footage, or local test).
   "test" footage can be rendered but never posted.
2. Human approval: nothing posts until a person approves it in the review UI.
3. Content: clips flagged as spoilers or unsafe are blocked from posting.
4. Pacing: per-platform daily caps, minimum gap between posts, quiet hours.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config as config_mod

PERMISSION_KINDS = ("campaign", "written", "own", "test")


def permission(channel_cfg: dict | None) -> tuple[str | None, str]:
    """Return (kind, detail) parsed from channel.permission, e.g. "campaign:foo"."""
    if not channel_cfg:
        return None, ""
    raw = str(channel_cfg.get("permission", "")).strip()
    kind, _, detail = raw.partition(":")
    kind = kind.strip().lower()
    if kind not in PERMISSION_KINDS:
        return None, ""
    if kind in ("campaign", "written") and not detail.strip():
        return None, ""
    return kind, detail.strip()


def can_record(channel_cfg: dict | None) -> tuple[bool, str]:
    kind, detail = permission(channel_cfg)
    if not kind:
        return False, (
            "no permission set. Add permission = \"campaign:<id>\", \"written:<link to the "
            "streamer's OK>\", \"own\" or \"test\" to this channel in config.toml"
        )
    return True, f"{kind}:{detail}" if detail else kind


def campaign_for(cfg: dict, channel_cfg: dict | None) -> dict | None:
    kind, detail = permission(channel_cfg)
    return config_mod.campaign(cfg, detail) if kind == "campaign" else None


def can_post(cfg: dict, channel_cfg: dict | None, clip: dict, platform: str) -> tuple[bool, str]:
    kind, detail = permission(channel_cfg)
    if not kind:
        return False, "channel has no permission"
    if kind == "test":
        return False, "test footage is never posted"
    if clip.get("status") != "approved":
        return False, "not approved by a human yet"
    if clip.get("spoiler_risk") == "high":
        return False, "flagged as a story spoiler"
    if clip.get("unsafe"):
        return False, "flagged unsafe"
    if kind == "campaign":
        camp = config_mod.campaign(cfg, detail)
        if not camp:
            return False, f"campaign '{detail}' is not defined in config"
        if camp.get("active") is False:
            return False, f"campaign '{detail}' is marked inactive"
        allowed = [p.lower() for p in camp.get("platforms", [])]
        if platform != "export" and allowed and platform not in allowed:
            return False, f"campaign '{detail}' doesn't pay on {platform}"
    return True, "ok"


def auto_gate(cfg: dict, channel_cfg: dict | None, clip: dict, notes: dict) -> tuple[bool, str]:
    """Checks a clip must pass before the AI may approve it on its own.
    These can't be switched off: anything failing them waits for a human."""
    kind, detail = permission(channel_cfg)
    if not kind:
        return False, "channel has no permission"
    if kind == "test":
        return False, "test footage is never posted"
    if not clip.get("final_path"):
        return False, "not rendered"
    if notes.get("source", "template") == "template":
        return False, "no AI spoiler/safety check (template packaging; set ANTHROPIC_API_KEY)"
    if notes.get("unsafe"):
        return False, "flagged unsafe"
    allowed = {"none"} | ({"low"} if cfg.get("auto_approve", {}).get("allow_spoiler_low") else set())
    risk = clip.get("spoiler_risk") or "unknown"
    if risk not in allowed:
        return False, f"spoiler risk is {risk}"
    if kind == "campaign":
        camp = config_mod.campaign(cfg, detail)
        if not camp:
            return False, f"campaign '{detail}' is not defined in config"
        if camp.get("active") is False:
            return False, f"campaign '{detail}' is marked inactive"
    return True, "ok"


def pacing_ok(cfg: dict, db, platform: str, now: float | None = None) -> tuple[bool, str]:
    now = now or time.time()
    posting = cfg["posting"]
    cap = int(posting.get("max_posts_per_day", {}).get(platform, 10))
    day_count = db.one(
        "SELECT COUNT(*) AS n FROM posts WHERE platform = ? AND posted_at > ? AND status != 'error'",
        (platform, now - 86400),
    )["n"]
    if day_count >= cap:
        return False, f"{platform}: daily cap {cap} reached"
    if platform != "export":
        last = db.one(
            "SELECT MAX(posted_at) AS t FROM posts WHERE platform = ? AND status != 'error'",
            (platform,),
        )["t"]
        gap = float(posting.get("min_gap_minutes", 45)) * 60
        if last and now - last < gap:
            return False, f"{platform}: next slot in {int((gap - (now - last)) / 60) + 1} min"
        quiet = set(posting.get("quiet_hours", []))
        if quiet:
            hour = datetime.fromtimestamp(now, ZoneInfo(posting.get("timezone", "UTC"))).hour
            if hour in quiet:
                return False, f"{platform}: quiet hours"
    return True, "ok"


def estimated_earnings(views: int, campaign: dict | None) -> float:
    """Pay for one post under a per-view campaign: rate per 1k, capped per clip,
    nothing below the campaign's minimum view count."""
    if not campaign:
        return 0.0
    if views < int(campaign.get("min_views", 0)):
        return 0.0
    pay = views / 1000.0 * float(campaign.get("rate_per_1k", 0))
    cap = campaign.get("cap_per_clip")
    if cap:
        pay = min(pay, float(cap))
    return round(pay, 2)
