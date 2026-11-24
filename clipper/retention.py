"""Deletes clip media nobody needs any more, so the disk can't fill up mid-launch and stop recording.

Stream segments are pruned by recorder.Pruner. This handles what's cut from them:

- raw cuts (data/raw, ~60 MB each) of clips that are posted, rejected or failed, after
  [retention].raw_hours (default 24). Nothing re-renders those clips any more.
- rendered videos (data/clips/<id>/*.mp4) of rejected and failed clips after rejected_days
  (default 3), and of posted clips after posted_days (default 14). Thumbnails stay for the dashboard.
- everything above, plus the data/export copy, as soon as a posted clip is up on YouTube or
  Instagram: the dashboard embeds it from there (Drive's copy goes too, see drive.prune_posted).

Queued, rendering, ready and approved clips are never touched, nor is a raw cut another live clip
still uses. Under [retention].min_free_gb (default 5) free, everything allowed is deleted right away
and Telegram says so (at most every 6 hours).
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from . import config as config_mod
from .db import DB

log = logging.getLogger(__name__)

DONE = ("posted", "rejected", "failed")
LIVE = ("queued", "rendering", "ready", "approved")
_last_alert = 0.0


def _settings(cfg: dict) -> dict:
    r = cfg.get("retention", {})
    return {"raw_hours": float(r.get("raw_hours", 24)), "rejected_days": float(r.get("rejected_days", 3)),
            "posted_days": float(r.get("posted_days", 14)), "min_free_gb": float(r.get("min_free_gb", 5))}


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def prune_media(db: DB, cfg: dict, now: float | None = None, low_disk: bool | None = None) -> dict:
    """One pass. Returns {"raw": n, "renders": n, "freed_mb": n}."""
    now = now or time.time()
    s = _settings(cfg)
    data = config_mod.data_dir(cfg)
    if low_disk is None:
        low_disk = data.exists() and free_gb(data) < s["min_free_gb"]
    hour, day = 3600, 86400
    raw_age = 0 if low_disk else s["raw_hours"] * hour
    out = {"raw": 0, "renders": 0, "freed_mb": 0.0}

    def remove(p: Path) -> None:
        try:
            size = p.stat().st_size
            p.unlink()
        except OSError:
            return
        out["freed_mb"] += size / 1e6

    marks = ",".join("?" for _ in LIVE)
    live = {r["source"] for r in db.query(f"SELECT source FROM clips WHERE status IN ({marks})", LIVE)}
    # up on YouTube/Instagram: the dashboard embeds it from there, so no copy of ours is needed
    online = {r["clip_id"] for r in db.query(
        "SELECT DISTINCT clip_id FROM posts WHERE platform IN ('youtube', 'instagram') "
        "AND status = 'published' AND url != ''")}
    done = ",".join("?" for _ in DONE)
    for c in db.query(f"SELECT id, status, source, final_path, updated_at FROM clips WHERE status IN ({done})", DONE):
        age = now - float(c["updated_at"] or 0)
        gone = c["status"] == "posted" and c["id"] in online
        src = Path(c["source"] or "")
        if (c["source"] and c["source"] not in live and (gone or age >= raw_age)
                and _inside(src, data / "raw") and src.is_file()):
            remove(src)
            out["raw"] += 1
        keep_days = s["posted_days"] if c["status"] == "posted" else s["rejected_days"]
        if c["final_path"] and (low_disk or gone or age >= keep_days * day):
            folder = Path(c["final_path"]).parent
            if _inside(folder, data / "clips"):
                videos = list(folder.glob("*.mp4"))
                for v in videos:
                    remove(v)
                out["renders"] += bool(videos)
        if gone:  # the data/export copy (video + caption) was for posting by hand
            for p in db.query("SELECT remote_id FROM posts WHERE clip_id = ? AND platform = 'export' "
                              "AND remote_id != ''", (c["id"],)):
                video = Path(p["remote_id"])
                if _inside(video, data / "export"):
                    for f in (video, video.with_suffix(".txt")):
                        if f.is_file():
                            remove(f)
    out["freed_mb"] = round(out["freed_mb"], 1)
    if out["raw"] or out["renders"]:
        log.info("retention: deleted %d raw cuts and %d rendered clips (%.0f MB)%s",
                 out["raw"], out["renders"], out["freed_mb"], " - disk was low" if low_disk else "")
    return out


def housekeeping(db: DB, cfg: dict, send=None) -> dict:
    """prune_media, plus a Telegram alert while free space stays under min_free_gb."""
    global _last_alert
    data = config_mod.data_dir(cfg)
    if not data.exists():
        return {}
    from .autoapprove import expire_stale

    if expired := expire_stale(db, cfg):
        log.info("expired %d unreviewed clip(s) past [auto_approve].expire_hours", len(expired))
    floor = _settings(cfg)["min_free_gb"]
    low = free_gb(data) < floor
    out = prune_media(db, cfg, low_disk=low)
    if low and time.time() - _last_alert > 6 * 3600:
        _last_alert = time.time()
        left = free_gb(data)
        text = (f"💾 Disk almost full: {left:.1f} GB free (alert under {floor:g} GB). Deleted "
                f"{out['raw']} old raw cuts and {out['renders']} old renders ({out['freed_mb']:.0f} MB). "
                "Recordings stop when the disk fills: free space or lower [record].retain_minutes.")
        log.error(text)
        try:
            from . import notify

            (send or notify.send)(text)
        except Exception:
            pass
    return out
