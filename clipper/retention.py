"""Deletes clip media nobody needs any more, so the disk can't fill up mid-launch and stop recording.

Stream segments are pruned by recorder.Pruner. This handles what's cut from them:

- raw cuts (data/raw, ~60 MB each) of clips that are posted, rejected or failed, after
  [retention].raw_hours (default 24). Nothing re-renders those clips any more.
- rendered videos (data/clips/<id>/*.mp4) of rejected and failed clips after rejected_days
  (default 3), and of posted clips after posted_days (default 14). Thumbnails stay for the dashboard.
- everything above, plus the data/export copy, as soon as a posted clip is up on YouTube or
  Instagram: the dashboard embeds it from there (Drive's copy goes too, see drive.prune_posted).
- rejected clips (by you, the AI, or expiry) completely, rows included: see purge_rejected.

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


def purge_rejected(db: DB, cfg: dict, drive_delete=None) -> int:
    """Delete rejected clips outright: Drive copy, raw cut, render folder (thumbnail too), export copy and
    rows. Storage is tight on the server and in Drive, and nobody goes back to a rejected clip."""
    if drive_delete is None:
        from .publishers import drive

        drive_delete = (lambda link: drive.delete(cfg, link)) if drive.enabled(cfg) else None
    data = config_mod.data_dir(cfg)
    # ids are reused once the newest rows go (no AUTOINCREMENT): leave rejected clips newer than the newest
    # kept one, so an old Telegram button can't act on a new clip. The next pass takes them.
    newest = db.one("SELECT MAX(id) AS m FROM clips WHERE status != 'rejected'")["m"] or 0
    keep_src = {r["source"] for r in db.query("SELECT source FROM clips WHERE status != 'rejected'")}
    gone: list[int] = []
    for c in db.query("SELECT id, source, final_path, drive_url FROM clips WHERE status = 'rejected' AND id < ?",
                      (newest,)):
        if c["drive_url"]:
            if not drive_delete:
                continue  # Drive is signed out: keep the row, or its link (and the file) is lost
            try:
                drive_delete(c["drive_url"])
            except Exception as exc:
                log.warning("clip %s: Drive delete failed, retrying next pass: %s", c["id"], str(exc)[:200])
                continue
        src = Path(c["source"] or "")
        if c["source"] and c["source"] not in keep_src and _inside(src, data / "raw") and src.is_file():
            src.unlink(missing_ok=True)
        if c["final_path"]:
            folder = Path(c["final_path"]).parent
            if folder != data / "clips" and _inside(folder, data / "clips"):
                shutil.rmtree(folder, ignore_errors=True)
        for p in db.query("SELECT remote_id FROM posts WHERE clip_id = ? AND platform = 'export' "
                          "AND remote_id != ''", (c["id"],)):
            video = Path(p["remote_id"])
            if _inside(video, data / "export"):
                video.unlink(missing_ok=True)
                video.with_suffix(".txt").unlink(missing_ok=True)
        gone.append(c["id"])
    if gone:
        marks = ",".join("?" * len(gone))
        tables = {t["name"] for t in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        db.execute(f"DELETE FROM posts WHERE clip_id IN ({marks})", gone)
        if "review_messages" in tables:
            db.execute(f"DELETE FROM review_messages WHERE clip_id IN ({marks})", gone)
        if "twitch_clips_seen" in tables:  # keep the row: the same Twitch clip isn't downloaded again
            db.execute(f"UPDATE twitch_clips_seen SET clip_id = NULL WHERE clip_id IN ({marks})", gone)
        db.execute(f"DELETE FROM clips WHERE id IN ({marks}) AND status = 'rejected'", gone)
        log.info("retention: deleted %d rejected clip(s) completely", len(gone))
    return len(gone)


def housekeeping(db: DB, cfg: dict, send=None) -> dict:
    """prune_media, plus a Telegram alert while free space stays under min_free_gb."""
    global _last_alert
    data = config_mod.data_dir(cfg)
    if not data.exists():
        return {}
    from .autoapprove import expire_stale

    if expired := expire_stale(db, cfg):
        log.info("expired %d unreviewed clip(s) past [auto_approve].expire_hours", len(expired))
    purge_rejected(db, cfg)
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
