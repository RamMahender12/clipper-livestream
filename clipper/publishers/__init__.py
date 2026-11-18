"""Publisher registry. Every platform publisher uses the official API only.

Each streamer can post to its own accounts (Reach-style campaigns want one
niche page per streamer). account_cfg() points a publisher at the channel's
account:

- youtube: channel youtube_token_file, default secrets/youtube_<login>.json
  (create it with `clipper auth youtube --channel <login>`)
- instagram: channel instagram_user_id (find it with `clipper instagram-accounts`)
- tiktok: channel tiktok_token_file

A channel with no account for a platform is never posted there, so one
streamer's clips can't land on another streamer's page.
"""
from __future__ import annotations

from .. import config as config_mod
from .base import PostMeta, PostResult, PostStats, Publisher, caption_with_tags
from .export import ExportPublisher
from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher

ALL = ["export", "youtube", "instagram", "tiktok"]

_REGISTRY: dict[str, type[Publisher]] = {
    "export": ExportPublisher,
    "youtube": YouTubePublisher,
    "instagram": InstagramPublisher,
    "tiktok": TikTokPublisher,
}

# platform -> (config section, key inside it, channel key that overrides it)
_ACCOUNT_KEYS = {
    "youtube": ("youtube", "token_file", "youtube_token_file"),
    "instagram": ("instagram", "ig_user_id", "instagram_user_id"),
    "tiktok": ("tiktok", "token_file", "tiktok_token_file"),
}


def channel_account(cfg: dict, channel_cfg: dict | None, platform: str) -> str:
    """The account a channel posts to on `platform`, or "" if it has none.
    Export has a single shared account ("export")."""
    if platform == "export":
        return "export"
    spec = _ACCOUNT_KEYS.get(platform)
    if not spec or not channel_cfg:
        return ""
    _, _, chan_key = spec
    value = str(channel_cfg.get(chan_key) or "").strip()
    if not value and platform == "youtube" and channel_cfg.get("login"):
        value = f"secrets/youtube_{channel_cfg['login'].lower()}.json"
        if not config_mod.resolve(cfg, value).exists():
            return ""  # not authorized yet
    if platform in ("youtube", "tiktok") and value and not config_mod.resolve(cfg, value).exists():
        return ""
    return value


def account_cfg(cfg: dict, channel_cfg: dict | None, platform: str) -> tuple[dict, str]:
    """A config copy whose platform section points at the channel's account."""
    account = channel_account(cfg, channel_cfg, platform)
    spec = _ACCOUNT_KEYS.get(platform)
    if not spec or not account:
        return cfg, account
    section, key, _ = spec
    out = dict(cfg)
    out[section] = {**cfg.get(section, {}), key: account}
    return out, account


def youtube_token_path(cfg: dict, channel_cfg: dict) -> str:
    """Where `clipper auth youtube --channel X` stores that channel's token."""
    return (channel_cfg.get("youtube_token_file")
            or f"secrets/youtube_{channel_cfg['login'].lower()}.json")


def get_publisher(name: str, cfg: dict, channel_cfg: dict | None = None) -> Publisher:
    key = (name or "").strip().lower()
    try:
        cls = _REGISTRY[key]
    except KeyError:
        raise ValueError(f"unknown publisher {name!r}; choose one of {', '.join(ALL)}") from None
    if channel_cfg is not None:
        cfg, _ = account_cfg(cfg, channel_cfg, key)
    return cls(cfg)


__all__ = [
    "ALL", "get_publisher", "account_cfg", "channel_account", "youtube_token_path",
    "Publisher", "PostMeta", "PostResult", "PostStats", "caption_with_tags",
]
