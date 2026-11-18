"""Config loading. Settings live in config.toml (copy config.example.toml).

Secrets never go in config.toml: tokens come from environment variables or
files under secrets/ (both git-ignored).
"""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "paths": {
        "data_dir": "data",
        "whisper_model": "models/ggml-base.en.bin",
    },
    "record": {
        "quality": "1080p60,1080p,720p60,best",
        "segment_seconds": 30,
        "retain_minutes": 90,
        "poll_seconds": 60,
        "keep_awake": True,  # macOS: block idle sleep while `clipper run` is running
    },
    "detect": {
        "window_seconds": 10,
        "baseline_windows": 30,
        "warmup_windows": 6,
        "min_ratio": 2.5,
        "min_z": 3.0,
        "min_messages": 12,
        "clip_mentions": 3,
        "cooldown_seconds": 45,
        "keywords": [
            "kekw", "lul", "lol", "lmao", "lmfao", "omegalul", "icant", "wtf",
            "no way", "noway", "pog", "poggers", "insane", "holy", "???", "bro",
            "w", "wow", "omg", "dead", "clip",
        ],
    },
    "clip": {
        "pre_seconds": 22,
        "post_seconds": 8,
        "chat_delay_seconds": 6,
        "layout": "blur",  # blur | facecam_split | crop
        "zoom": 1.35,  # blur layout: >1 crops the sides so gameplay fills more of the frame
        "width": 1080,
        "height": 1920,
        "fps": 60,
        "encoder": "libx264",  # libx264 | h264_videotoolbox
        "max_per_hour_per_channel": 12,
        "render_workers": 1,
    },
    "captions": {
        "enabled": True,
        "words_per_chunk": 3,
        "font": "Arial Black",
        "font_size": 84,
        "highlight_color": "&H0000E5FF",  # ASS BGR: yellow
        "hook_seconds": 3.0,
    },
    "ai": {
        "enabled": True,
        "model": "claude-opus-5-5",
        "effort": "medium",
        "page_handle": "@yourpage",
        "niche": "GTA 6 launch-week clips",
        "voice": "short, punchy, funny, never mean; no spoilers",
    },
    "review": {"host": "127.0.0.1", "port": 8765},
    "posting": {
        "platforms": ["export"],
        "min_gap_minutes": 45,
        "max_posts_per_day": {"youtube": 10, "instagram": 8, "tiktok": 5, "export": 1000},
        "quiet_hours": [],  # e.g. [2, 3, 4, 5] local hours with no posting
        "timezone": "Asia/Kolkata",
        # GTA 6's first official unlock (New Zealand). Live clips recorded earlier are never posted.
        "earliest_footage": "2026-11-18T11:00:00+00:00",
    },
    "stats": {"refresh_minutes": 120},
    "gemini": {
        "enabled": True,  # second reviewer that watches the rendered clip (needs GEMINI_API_KEY)
        "model": "gemini-3.8-flash",
        "backend": "vertex",  # vertex (Cloud project + service account) | ai_studio (API key, prepay)
        "service_account_file": "secrets/vertex_sa.json",
        "vertex_project": "",  # empty = the service account's own project
        "vertex_location": "global",
        "api_key_env": "GEMINI_API_KEY",
        "thinking_level": "low",
        "weight": 0.6,  # share of the final score from Gemini's visual score
        "disagree_points": 3,  # scores this far apart are held for you
    },
    "review_bot": {
        "enabled": True,  # approve/reject clips from Telegram (needs TELEGRAM_REVIEW_* secrets)
        "max_per_hour": 12,  # clips sent for review per hour (respects quiet hours)
        "per_push": 3,  # clips sent per minute while catching up
        "push_every_seconds": 60,
        "dashboard_url": "https://gta6clipper.dawnlights.in/",
    },
    "campaign_watch": {
        "enabled": True,  # Telegram alert when a new matching Content Rewards campaign appears
        "interval_minutes": 20,
        "keywords": ["gta", "grand theft auto"],
        "platforms_any": ["youtube", "instagram"],  # skip campaigns that pay only on TikTok/X
        "max_pages_per_run": 15,
        "page_delay_seconds": 2,
    },
    "auto_approve": {
        "enabled": False,
        "interval_minutes": 3,
        "batch": 10,  # clips reviewed per pass
        "min_history": 15,  # posts with view data before the stats model counts
        "mature_hours": 24,  # a post's views count once it's this old
        "min_percentile": 0.45,  # predicted to beat 45% of past posts
        "min_confidence": 7,  # Claude's confidence, 1-10
        "cold_start_min_quality": 8,  # stricter bar until there's history
        "max_per_day": 20,
        "cold_start_max_per_day": 6,
        "auto_reject": True,
        "expire_hours": 12,  # unposted ready clips older than this are dropped
        "allow_spoiler_low": False,
        "veto_minutes": 10,  # auto-approved clips wait this long before posting
        "effort": "high",
        "examples": 6,  # best/worst past posts shown to Claude
    },
    "youtube": {
        "client_secrets_file": "secrets/youtube_client_secret.json",
        "token_file": "secrets/youtube_token.json",
        "privacy": "public",
        "category_id": "20",
    },
    "instagram": {
        "ig_user_id": "",
        "access_token_env": "IG_ACCESS_TOKEN",
        "graph_version": "v27.0",
        "share_to_feed": True,
    },
    "tiktok": {
        "client_key_env": "TIKTOK_CLIENT_KEY",
        "client_secret_env": "TIKTOK_CLIENT_SECRET",
        "token_file": "secrets/tiktok_token.json",
        "mode": "inbox",  # inbox = lands in TikTok drafts; direct needs an audited app
    },
    "campaigns": [],
    "channels": [],
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.toml"
    if not path.exists():
        example = ROOT / "config.example.toml"
        raise SystemExit(
            f"No config at {path}. Copy {example.name} to config.toml and edit it."
        )
    with open(path, "rb") as fh:
        user = tomllib.load(fh)
    cfg = _merge(DEFAULTS, user)
    cfg["_path"] = str(path)
    _apply_env_overrides(cfg)
    return cfg


# Settings that must differ inside the cloud container (set in docker-compose),
# so one config.toml works on the Mac and on the server.
ENV_OVERRIDES = {
    "CLIPPER_REVIEW_HOST": ("review", "host"),
    "CLIPPER_CAPTION_FONT": ("captions", "font"),
    "CLIPPER_ENCODER": ("clip", "encoder"),
}


def _apply_env_overrides(cfg: dict) -> None:
    import os

    for env, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env)
        if value:
            cfg[section][key] = value


def data_dir(cfg: dict) -> Path:
    p = Path(cfg["paths"]["data_dir"])
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve(cfg: dict, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def campaign(cfg: dict, campaign_id: str | None) -> dict | None:
    if not campaign_id:
        return None
    for c in cfg.get("campaigns", []):
        if c.get("id") == campaign_id:
            return c
    return None


def channel(cfg: dict, login: str) -> dict | None:
    for ch in cfg.get("channels", []):
        if ch.get("login", "").lower() == login.lower():
            return ch
    return None
