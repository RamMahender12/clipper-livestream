"""Publisher interface. Every uploader uses the platform's official API only.

No browser automation, no unofficial endpoints, no account farms: those are
the fastest way to get banned and they break the platforms' terms.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PostMeta:
    title: str  # YouTube title (<= 100 chars)
    caption: str  # full description / caption, credit + disclosure included
    hashtags: list[str] = field(default_factory=list)
    is_paid_promotion: bool = False  # True when a campaign pays per view -> platform disclosure toggles
    clip_id: int | None = None


@dataclass
class PostResult:
    ok: bool
    platform: str
    remote_id: str = ""
    url: str = ""
    status: str = ""  # "published" | "draft" | "exported" | "error"
    error: str = ""


@dataclass
class PostStats:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0


class Publisher:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def ready(self) -> tuple[bool, str]:
        """Return (ok, message) describing whether credentials are configured."""
        raise NotImplementedError

    def publish(self, video: Path, meta: PostMeta) -> PostResult:
        raise NotImplementedError

    def fetch_stats(self, remote_id: str) -> PostStats | None:
        return None

    def check_status(self, remote_id: str) -> tuple[str, str]:
        """(state, detail) of a live post: ok, removed, rejected, blocked, age, or unknown."""
        return "unknown", "not supported for this platform"


def caption_with_tags(meta: PostMeta, limit: int = 2200) -> str:
    text = meta.caption.strip()
    present = {w.lower() for w in text.split() if w.startswith("#")}
    tags = []
    for t in meta.hashtags:
        tag = t if t.startswith("#") else f"#{t}"
        if tag.lower() not in present and tag not in tags:  # skip tags already in the caption
            tags.append(tag)
    if tags:
        text = f"{text}\n\n{' '.join(tags)}"
    return text[:limit]
