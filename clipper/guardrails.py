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


def before_release(cfg: dict, clip: dict) -> str:
    """Non-empty reason if a live clip was recorded before [posting].earliest_footage
    (GTA 6's first official unlock). Rockstar takes down pre-release footage."""
    cutoff = cfg.get("posting", {}).get("earliest_footage")
    wall = clip.get("wall_start")
    if not cutoff or wall is None:
        return ""
    if float(wall) < datetime.fromisoformat(cutoff).timestamp():
        return f"recorded before {cutoff} (pre-release footage)"
    return ""


def can_post(cfg: dict, channel_cfg: dict | None, clip: dict, platform: str) -> tuple[bool, str]:
    kind, detail = permission(channel_cfg)
    if not kind:
        return False, "channel has no permission"
    if kind == "test":
        return False, "test footage is never posted"
    if clip.get("status") != "approved":
        return False, "not approved by a human yet"
    if why := before_release(cfg, clip):
        return False, why
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


def gemini_label(gem: dict | None) -> str:
    from .gemini import label

    return label(gem)


def off_game(cfg: dict, notes: dict) -> str:
    """Why the clip is dropped as not GTA 6 gameplay (giveaways, IRL, chatting, other games), or "".
    Every campaign here pays for GTA 6 clips, so these can never post; holding them for you only
    filled the queue (19 Nov: ~104 of 161 held clips). [gemini].reject_off_game = false holds them."""
    gem = notes.get("gemini") or {}
    if gem.get("is_gta6") is not False or not cfg.get("gemini", {}).get("reject_off_game", True):
        return ""
    seen = (gem.get("what_happens") or "").strip()
    return f"not GTA 6 gameplay ({gemini_label(gem)}){': ' + seen[:200] if seen else ''}"


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
    if why := before_release(cfg, clip):
        return False, why
    if notes.get("source", "template") == "template":
        return False, "no AI spoiler/safety check (template packaging; set ANTHROPIC_API_KEY)"
    if notes.get("unsafe"):
        return False, "flagged unsafe"
    if notes.get("disagree"):
        g = notes.get("gemini") or {}
        return False, (f"Claude ({notes.get('claude_quality')}/10) and Gemini ({g.get('quality')}/10) "
                       "disagree; your call")
    gem = notes.get("gemini") or {}
    if not gem and cfg.get("gemini", {}).get("enabled"):
        return False, ("no video check yet (it looks for age-restriction risk and that it's GTA 6)"
                       + (f": {notes['gemini_error'][:90]}" if notes.get("gemini_error") else ""))
    name = gemini_label(gem)
    if gem.get("hook_matches") is False:
        return False, f"{name} says the hook doesn't match the video"
    if gem:
        if "age_risk" not in gem or "violence" not in gem:
            return False, "needs a fresh video check (age-restriction, violence and game checks)"
        if notes.get("violence_mostly"):
            return False, "mostly violence: nothing clean enough to keep"
        if gem.get("violence"):
            return False, "still shows violence: " + "; ".join(str(v[2]) for v in gem["violence"][:2])
        if notes.get("music_heavy") or gem.get("music") == "likely":
            why = gem.get("music_reason") or "radio or song playing through most of it"
            return False, f"licensed music likely ({why}): YouTube may claim it, Instagram may mute it"
        if gem.get("is_gta6") is False:
            return False, f"{name} doesn't think this is GTA 6 gameplay"
        if gem.get("streamer_featured") is False:
            return False, "the streamer isn't part of the moment (campaign rule)"
        if gem.get("age_risk") == "high":
            return False, f"YouTube may age-restrict it ({gem.get('age_reason') or 'violence, sex or swearing'})"
        min_gem = int(cfg.get("auto_approve", {}).get("min_gemini_quality", 0))
        if int(gem.get("quality") or 0) < min_gem:
            return False, f"{name}, which looked at the video, rates it {gem.get('quality')}/10 (needs {min_gem})"
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


def pacing_ok(cfg: dict, db, platform: str, now: float | None = None,
              account: str | None = None) -> tuple[bool, str]:
    """Daily cap and minimum gap per account (per platform if no account given)."""
    now = now or time.time()
    posting = cfg["posting"]
    cap = int(posting.get("max_posts_per_day", {}).get(platform, 10))
    where, args = "platform = ?", [platform]
    label = platform
    if account is not None:
        where += " AND account = ?"
        args.append(account)
        label = f"{platform} ({account.rsplit('/', 1)[-1]})"
    day_count = db.one(
        f"SELECT COUNT(*) AS n FROM posts WHERE {where} AND posted_at > ? AND status != 'error'",
        (*args, now - 86400),
    )["n"]
    if day_count >= cap:
        return False, f"{label}: daily cap {cap} reached"
    if platform != "export":
        last = db.one(
            f"SELECT MAX(posted_at) AS t FROM posts WHERE {where} AND status != 'error'",
            tuple(args),
        )["t"]
        gap = float(posting.get("min_gap_minutes", 45)) * 60
        if last and now - last < gap:
            return False, f"{label}: next slot in {int((gap - (now - last)) / 60) + 1} min"
        quiet = set(posting.get("quiet_hours", []))
        if quiet:
            hour = datetime.fromtimestamp(now, ZoneInfo(posting.get("timezone", "UTC"))).hour
            if hour in quiet:
                return False, f"{label}: quiet hours"
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
