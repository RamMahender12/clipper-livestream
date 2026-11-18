"""Local export: copies the final MP4 plus a caption sidecar into
data/export/<YYYY-MM-DD>/ so a human can post it by hand (or hand it to a
campaign dashboard). No network, no credentials, always ready.
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path

from .. import config as config_mod
from .base import Publisher, PostMeta, PostResult, caption_with_tags


def slugify(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].rstrip("-") or "clip"


def _today(cfg: dict) -> str:
    tz_name = cfg.get("posting", {}).get("timezone")
    try:
        from zoneinfo import ZoneInfo

        now = dt.datetime.now(ZoneInfo(tz_name)) if tz_name else dt.datetime.now()
    except Exception:  # unknown zone / missing tzdata: fall back to local time
        now = dt.datetime.now()
    return now.strftime("%Y-%m-%d")


def export_paths(cfg: dict, meta: PostMeta) -> tuple[Path, Path]:
    """Return (video_path, caption_path) for this clip; pure apart from data_dir mkdir."""
    folder = config_mod.data_dir(cfg) / "export" / _today(cfg)
    clip_id = meta.clip_id if meta.clip_id is not None else "clip"
    stem = f"{clip_id}_{slugify(meta.title)}"
    return folder / f"{stem}.mp4", folder / f"{stem}.txt"


def sidecar_text(meta: PostMeta) -> str:
    lines = [meta.title.strip(), "", caption_with_tags(meta)]
    if meta.is_paid_promotion:
        lines += ["", "[PAID PROMOTION] Turn on the platform's paid-partnership / "
                      "branded-content disclosure toggle when posting this clip."]
    return "\n".join(lines).rstrip() + "\n"


class ExportPublisher(Publisher):
    name = "export"

    def ready(self) -> tuple[bool, str]:
        return True, "export: writes files to data/export/<date>/"

    def publish(self, video: Path, meta: PostMeta) -> PostResult:
        try:
            video = Path(video)
            if not video.is_file():
                return PostResult(ok=False, platform=self.name, status="error",
                                  error=f"video not found: {video}")
            dest, txt = export_paths(self.cfg, meta)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if video.resolve() != dest.resolve():
                shutil.copy2(video, dest)
            txt.write_text(sidecar_text(meta), encoding="utf-8")
            return PostResult(ok=True, platform=self.name, remote_id=str(dest),
                              url=dest.resolve().as_uri(), status="exported")
        except OSError as exc:
            return PostResult(ok=False, platform=self.name, status="error",
                              error=f"export failed: {exc}"[:300])
