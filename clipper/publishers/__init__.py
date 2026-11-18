"""Publisher registry. Every platform publisher uses the official API only."""
from __future__ import annotations

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


def get_publisher(name: str, cfg: dict) -> Publisher:
    key = (name or "").strip().lower()
    try:
        cls = _REGISTRY[key]
    except KeyError:
        raise ValueError(f"unknown publisher {name!r}; choose one of {', '.join(ALL)}") from None
    return cls(cfg)


__all__ = [
    "ALL", "get_publisher", "Publisher", "PostMeta", "PostResult", "PostStats",
    "caption_with_tags",
]
