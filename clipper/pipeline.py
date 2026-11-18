"""Clip job processing: transcribe -> write hook/caption -> burn captions -> render."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from . import captions, config as config_mod, guardrails, metadata, render
from .db import DB

log = logging.getLogger(__name__)


def clips_dir(cfg: dict) -> Path:
    d = config_mod.data_dir(cfg) / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


def queue_clip(
    db: DB,
    cfg: dict,
    *,
    channel: str,
    source: Path,
    start_s: float,
    duration_s: float,
    score: float = 0.0,
    spike_id: int | None = None,
    wall_start: float | None = None,
) -> int:
    ch = config_mod.channel(cfg, channel) or {"login": channel}
    camp = guardrails.campaign_for(cfg, ch)
    now = time.time()
    return db.insert(
        "clips",
        spike_id=spike_id,
        channel=channel,
        campaign_id=(camp or {}).get("id", ""),
        source=str(source),
        wall_start=wall_start,
        start_s=start_s,
        duration_s=duration_s,
        score=score,
        status="queued",
        created_at=now,
        updated_at=now,
    )


def process_clip(db: DB, cfg: dict, clip: dict) -> None:
    clip_id = clip["id"]
    ch = config_mod.channel(cfg, clip["channel"]) or {"login": clip["channel"]}
    camp = guardrails.campaign_for(cfg, ch)
    src = Path(clip["source"])
    if not src.exists():
        raise FileNotFoundError(f"source missing: {src}")
    start, dur = float(clip["start_s"] or 0), float(clip["duration_s"] or 30)
    ccfg, capcfg = cfg["clip"], cfg["captions"]

    # 1. transcript (cached across re-renders)
    if clip["words_json"]:
        words = [captions.Word(**w) for w in json.loads(clip["words_json"])]
    else:
        model = config_mod.resolve(cfg, cfg["paths"]["whisper_model"])
        try:
            words = captions.transcribe(src, model, start, dur) if model.exists() else []
        except Exception as exc:  # captions are nice-to-have; never lose the clip
            log.warning("clip %s: transcription failed: %s", clip_id, exc)
            words = []
        db.update("clips", clip_id,
                  words_json=json.dumps([w.__dict__ for w in words]),
                  transcript=captions.transcript_text(words))

    # 2. packaging (hook/title/caption) unless a human already edited it
    if not clip["hook"]:
        spike = db.one("SELECT * FROM spikes WHERE id = ?", (clip["spike_id"],)) if clip["spike_id"] else None
        terms = (spike["top_terms"].split(",") if spike and spike["top_terms"] else [])
        sample = json.loads(spike["sample"]) if spike and spike["sample"] else []
        meta = metadata.generate(cfg, ch, camp, captions.transcript_text(words), terms, sample,
                                 context=clip.get("source_note") or "")
        db.update(
            "clips", clip_id,
            hook=meta.hook, title=meta.title, caption=meta.caption,
            hashtags=",".join(meta.hashtags), spoiler_risk=meta.spoiler_risk,
            quality=meta.quality,
            ai_notes=json.dumps({"take": meta.take, "why": meta.why, "unsafe": meta.unsafe,
                                 "source": meta.source,
                                 # Claude's own verdict, kept apart from the combined one
                                 "claude_quality": meta.quality, "claude_spoiler": meta.spoiler_risk,
                                 "claude_unsafe": meta.unsafe}),
        )
        hook = meta.hook
    else:
        hook = clip["hook"].strip()  # " " means a human cleared it on purpose

    # 3. captions + render
    out_dir = clips_dir(cfg) / f"{clip_id:05d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    ass_path = None
    if capcfg.get("enabled", True):
        credit = ch.get("credit") or f"twitch.tv/{ch.get('login', '')}"
        ass = captions.build_ass(
            words,
            width=ccfg["width"], height=ccfg["height"],
            font=capcfg["font"], font_size=capcfg["font_size"],
            highlight=capcfg["highlight_color"], per_chunk=capcfg["words_per_chunk"],
            hook=hook, hook_seconds=capcfg["hook_seconds"],
            credit=credit if ch.get("credit_overlay", True) else "", duration=dur,
        )
        ass_path = out_dir / "captions.ass"
        ass_path.write_text(ass, encoding="utf-8")
    final = out_dir / "final.mp4"
    render.render(
        src, final, start, dur,
        layout=ch.get("layout") or ccfg["layout"],
        width=ccfg["width"], height=ccfg["height"], fps=ccfg["fps"],
        encoder=ccfg["encoder"], facecam=ch.get("facecam"), ass_path=ass_path,
        zoom=float(ch.get("zoom", ccfg.get("zoom", 1.0))),
    )
    try:
        render.thumbnail(final, out_dir / "thumb.jpg", at=min(1.5, dur / 2))
    except Exception:
        pass
    db.update("clips", clip_id, final_path=str(final), error="")
    gemini_review(db, cfg, clip_id)  # second opinion on the finished video
    db.update("clips", clip_id, status="ready")
    log.info("clip %s ready: %s", clip_id, final)


def gemini_review(db: DB, cfg: dict, clip_id: int) -> bool:
    """Have Gemini watch the rendered clip and merge its verdict with Claude's.
    Never fails the clip: without Gemini, Claude's verdict stands."""
    from . import gemini

    if not gemini.configured(cfg):
        return False
    row = dict(db.one("SELECT * FROM clips WHERE id = ?", (clip_id,)))
    if not row.get("final_path") or not Path(row["final_path"]).exists():
        return False
    try:
        notes = json.loads(row.get("ai_notes") or "{}")
    except json.JSONDecodeError:
        notes = {}
    ch = config_mod.channel(cfg, row["channel"]) or {"login": row["channel"]}
    spike = db.one("SELECT top_terms FROM spikes WHERE id = ?", (row["spike_id"],)) if row.get("spike_id") else None
    try:
        gem = gemini.review(cfg, Path(row["final_path"]),
                            streamer=ch.get("display_name") or row["channel"],
                            hook=(row.get("hook") or "").strip(), transcript=row.get("transcript") or "",
                            chat_terms=(spike["top_terms"].split(",") if spike and spike["top_terms"]
                                        else [row["source_note"]] if row.get("source_note") else []))
    except Exception as exc:  # network, quota, parse: keep Claude's verdict
        log.warning("clip %s: gemini review failed: %s", clip_id, exc)
        notes["gemini_error"] = str(exc)[:200]
        db.update("clips", clip_id, ai_notes=json.dumps(notes))
        return False
    claude_q = int(notes.get("claude_quality", row.get("quality") or 0))
    claude_spoiler = notes.get("claude_spoiler", row.get("spoiler_risk") or "unknown")
    claude_unsafe = bool(notes.get("claude_unsafe", notes.get("unsafe", False)))
    g = cfg["gemini"]
    final = gemini.combine(claude_q, claude_spoiler, claude_unsafe, gem,
                           weight=float(g.get("weight", 0.6)), disagree_points=int(g.get("disagree_points", 3)))
    notes.update({"claude_quality": claude_q, "claude_spoiler": claude_spoiler, "claude_unsafe": claude_unsafe,
                  "gemini": gem, "disagree": final["disagree"], "unsafe": final["unsafe"]})
    notes.pop("gemini_error", None)
    db.update("clips", clip_id, quality=final["quality"], spoiler_risk=final["spoiler"],
              ai_notes=json.dumps(notes))
    log.info("clip %s: Claude %s + Gemini %s -> %s (spoiler %s%s)", clip_id, claude_q, gem["quality"],
             final["quality"], final["spoiler"], ", models disagree" if final["disagree"] else "")
    return True


class RenderWorker(threading.Thread):
    def __init__(self, db: DB, cfg: dict, stop: threading.Event, idx: int = 0):
        super().__init__(daemon=True, name=f"render-{idx}")
        self.db, self.cfg, self.stop_event = db, cfg, stop

    def run(self) -> None:
        while not self.stop_event.is_set():
            row = self.db.claim_clip("queued", "rendering")
            if not row:
                self.stop_event.wait(2)
                continue
            try:
                process_clip(self.db, self.cfg, dict(row))
            except Exception as exc:
                log.exception("clip %s failed", row["id"])
                self.db.update("clips", row["id"], status="failed", error=str(exc)[:1000])


def drain_queue(db: DB, cfg: dict) -> int:
    """Render every queued clip in this thread (used by one-shot CLI commands)."""
    n = 0
    while True:
        row = db.claim_clip("queued", "rendering")
        if not row:
            return n
        try:
            process_clip(db, cfg, dict(row))
            n += 1
        except Exception as exc:
            log.exception("clip %s failed", row["id"])
            db.update("clips", row["id"], status="failed", error=str(exc)[:1000])
