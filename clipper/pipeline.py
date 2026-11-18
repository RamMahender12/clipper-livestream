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

    # 1. transcript (cached across re-renders)
    if clip["words_json"]:
        words = [captions.Word(**w) for w in json.loads(clip["words_json"])]
        words, _ = captions.split_markers(words)  # older transcripts kept [MUSIC PLAYING] as words
    else:
        model = config_mod.resolve(cfg, cfg["paths"]["whisper_model"])
        music: list = []
        try:
            words, music = captions.transcribe_full(src, model, start, dur) if model.exists() else ([], [])
        except Exception as exc:  # captions are nice-to-have; never lose the clip
            log.warning("clip %s: transcription failed: %s", clip_id, exc)
            words = []
        db.update("clips", clip_id,
                  words_json=json.dumps([w.__dict__ for w in words]),
                  transcript=captions.transcript_text(words), music_json=json.dumps(music))

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
        # a weak chat-spike moment stops here: no render, video check or judge. Claude's transcript
        # score of 4 or below never became a postable chat clip in launch-day data (0 of 38).
        # Viewer clips always go on: Claude underrates visual moments it can only read about.
        floor = int(cfg["ai"].get("min_quality_to_render", 0))
        if clip.get("spike_id") and meta.source != "template" and meta.quality < floor:
            db.update("clips", clip_id, status="rejected", decided_by="auto", decided_at=time.time(),
                      decision_note=f"Weak moment: Claude rated it {meta.quality}/10 from the transcript "
                                    f"(needs {floor} to be rendered and reviewed)")
            log.info("clip %s: skipped, Claude rated it %s/10", clip_id, meta.quality)
            return
    else:
        hook = clip["hook"].strip()  # " " means a human cleared it on purpose

    # 3. captions + render, then Gemini watches the result
    render_final(db, cfg, clip_id, ch, src, start, dur, words, hook)
    gemini_review(db, cfg, clip_id)  # second opinion on the finished video
    if cut_violence(db, cfg, clip_id) == "mostly":
        db.update("clips", clip_id, status="rejected", decided_by="auto", decided_at=time.time(),
                  decision_note="Dropped: mostly violence (YouTube age-restricts it), nothing clean enough to keep")
        log.info("clip %s dropped: mostly violence", clip_id)
        return
    if not clip["hook"]:  # our own hook (never one you wrote): fix it if the video check says it misleads
        swap_hook(db, cfg, clip_id)
    db.update("clips", clip_id, status="ready")
    log.info("clip %s ready", clip_id)


def clean_window(duration: float, spans: list, pad: float) -> tuple[float, float]:
    """The longest stretch of [0, duration] that avoids every (padded) violent span."""
    bad = sorted((max(0.0, a - pad), min(duration, b + pad)) for a, b, *_ in spans)
    best, cursor = (0.0, 0.0), 0.0
    for a, b in bad:
        if a - cursor > best[1] - best[0]:
            best = (cursor, a)
        cursor = max(cursor, b)
    if duration - cursor > best[1] - best[0]:
        best = (cursor, duration)
    return round(best[0], 2), round(best[1], 2)


def cut_violence(db: DB, cfg: dict, clip_id: int) -> str:
    """Trim violent moments (and stretches whisper marked as music) out: keep the longest clean
    stretch, re-render it and check it again. Returns "" (nothing to do), "cut", or "mostly" (too
    little left without the violence: drop the clip). Music running through most of the clip isn't
    cut; the clip is marked music_heavy and waits for you. Once per clip; never touches a clip you
    decided on."""
    vcfg = cfg.get("violence", {})
    if not vcfg.get("cut", True):
        return ""
    row = dict(db.one("SELECT * FROM clips WHERE id = ?", (clip_id,)))
    try:
        notes = json.loads(row.get("ai_notes") or "{}")
    except json.JSONDecodeError:
        return ""
    violent = (notes.get("gemini") or {}).get("violence") or []
    words, legacy_music = captions.split_markers(
        [captions.Word(**w) for w in json.loads(row.get("words_json") or "[]")])
    mcfg = cfg.get("music", {})
    music = []
    if mcfg.get("cut", True):
        music = [[x, y, "radio music"] for x, y in (json.loads(row.get("music_json") or "[]") or legacy_music)
                 if y - x >= float(mcfg.get("min_seconds", 1.0))]
    if (not violent and not music) or notes.get("violence_cut") or row.get("decided_by") == "human":
        return ""
    dur = float(row.get("duration_s") or 30)
    pad = float(vcfg.get("pad_seconds", 1.5))
    need = max(float(vcfg.get("min_keep_seconds", 6)), float(vcfg.get("min_keep_share", 0.4)) * dur)

    def fits(spans):
        w = clean_window(dur, spans, pad)
        return w if w[1] - w[0] >= need else None

    if violent and not fits(violent):
        notes["violence_mostly"] = True
        db.update("clips", clip_id, ai_notes=json.dumps(notes))
        return "mostly"
    removed, window = violent + music, fits(violent + music)
    if window is None:  # the music runs through most of it: cut only the violence, and hold it for you
        notes["music_heavy"] = True
        removed, window = violent, (fits(violent) if violent else None)
        if window is None:
            db.update("clips", clip_id, ai_notes=json.dumps(notes))
            return ""
    a, b = window
    keep = b - a
    if keep >= dur - 0.05:  # nothing inside the clip to cut
        return ""
    if not row.get("source") or not Path(row["source"]).exists():
        return ""
    kept = [captions.Word(round(w.start - a, 3), round(w.end - a, 3), w.text) for w in words if w.end > a and w.start < b]
    before = {k: row[k] for k in ("start_s", "duration_s", "words_json", "transcript", "final_path", "music_json")}
    left = [[round(max(x, a) - a, 2), round(min(y, b) - a, 2)]
            for x, y, *_ in (json.loads(row.get("music_json") or "[]") or legacy_music) if y > a and x < b]
    first = Path(row["final_path"]) if row.get("final_path") else None
    backup = first.with_name("final.uncut.mp4") if first and first.exists() else None
    if backup:
        first.replace(backup)
    notes["violence_cut"] = {"removed": removed, "kept": [a, b], "was": round(dur, 2)}
    db.update("clips", clip_id, start_s=float(row["start_s"] or 0) + a, duration_s=round(keep, 2),
              words_json=json.dumps([w.__dict__ for w in kept]), transcript=captions.transcript_text(kept),
              music_json=json.dumps(left), ai_notes=json.dumps(notes))
    ch = config_mod.channel(cfg, row["channel"]) or {"login": row["channel"]}
    try:
        render_final(db, cfg, clip_id, ch, Path(row["source"]), float(row["start_s"] or 0) + a, keep, kept,
                     (row.get("hook") or "").strip())
    except Exception as exc:  # keep the uncut render, held for you
        log.warning("clip %s: re-render without the violence failed: %s", clip_id, exc)
        if backup:
            backup.replace(first)
        notes.pop("violence_cut")
        db.update("clips", clip_id, ai_notes=json.dumps(notes), **before)
        return ""
    if backup:
        backup.unlink(missing_ok=True)
    gemini_review(db, cfg, clip_id)  # is it clean now, and does the hook still fit?
    log.info("clip %s: cut violence, kept %.1f-%.1fs of %.1fs", clip_id, a, b, dur)
    return "cut"


def render_final(db: DB, cfg: dict, clip_id: int, ch: dict, src: Path, start: float, dur: float,
                 words: list, hook: str) -> Path:
    ccfg, capcfg = cfg["clip"], cfg["captions"]
    words, _ = captions.split_markers(words)  # sound markers never go on screen
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
            censor=capcfg.get("censor_profanity", True),
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
        mute=captions.profane_spans(words) if capcfg.get("mute_profanity", True) else None,
    )
    try:
        render.thumbnail(final, out_dir / "thumb.jpg", at=min(1.5, dur / 2))
    except Exception:
        pass
    db.update("clips", clip_id, final_path=str(final), error="")
    return final


def swap_hook(db: DB, cfg: dict, clip_id: int) -> bool:
    """The video check looked at the clip and wrote its own hook and title. Use them when our hook
    doesn't match what happens, or (sharpen_hooks) when the clip is good enough to be worth a sharper
    one: the title is just updated, the hook is burned in, so the clip is re-rendered and checked
    again. Once per clip, never on a clip you decided on or a hook you wrote."""
    g = cfg["gemini"]
    if not g.get("swap_hook", True):
        return False
    row = dict(db.one("SELECT * FROM clips WHERE id = ?", (clip_id,)))
    try:
        notes = json.loads(row.get("ai_notes") or "{}")
    except json.JSONDecodeError:
        return False
    gem = notes.get("gemini") or {}
    if row.get("decided_by") == "human" or notes.get("hook_swapped"):
        return False
    good_enough = int(row.get("quality") or 0) >= int(g.get("sharpen_min_quality", 5))
    mismatch = gem.get("hook_matches") is False
    sharpen = bool(g.get("sharpen_hooks", True)) and good_enough
    title = (gem.get("better_title") or "").strip()
    if title and (sharpen or mismatch) and title != (row.get("title") or "").strip() and not notes.get("title_swapped"):
        notes["title_swapped"] = {"from": row.get("title") or "", "to": title}
        db.update("clips", clip_id, title=title, ai_notes=json.dumps(notes))
        row["title"] = title
    new = (gem.get("better_hook") or "").strip()
    old = (row.get("hook") or "").strip()
    if not new or new.lower() == old.lower() or not (mismatch or sharpen):
        return False
    if not row.get("source") or not Path(row["source"]).exists():
        return False
    ch = config_mod.channel(cfg, row["channel"]) or {"login": row["channel"]}
    words = [captions.Word(**w) for w in json.loads(row.get("words_json") or "[]")]
    first = Path(row["final_path"]) if row.get("final_path") else None
    backup = first.with_name("final.first.mp4") if first and first.exists() else None
    if backup:
        first.replace(backup)
    notes["hook_swapped"] = {"from": old, "to": new, "why": "mismatch" if mismatch else "sharper"}
    db.update("clips", clip_id, hook=new, ai_notes=json.dumps(notes))
    try:
        render_final(db, cfg, clip_id, ch, Path(row["source"]), float(row["start_s"] or 0),
                     float(row["duration_s"] or 30), words, new)
    except Exception as exc:  # keep the first render rather than losing the clip
        log.warning("clip %s: re-render with Gemini's hook failed: %s", clip_id, exc)
        if backup:
            backup.replace(first)
        notes.pop("hook_swapped")
        db.update("clips", clip_id, hook=old, ai_notes=json.dumps(notes), final_path=row["final_path"])
        return False
    if backup:
        backup.unlink(missing_ok=True)
    gemini_review(db, cfg, clip_id)
    log.info("clip %s: hook swapped to Gemini's %r (was %r)", clip_id, new, old)
    return True


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
    # never send a reviewer what Claude already flagged (slurs, sexual content): it's held for you anyway,
    # and repeatedly uploading that kind of footage is how a provider account gets flagged
    if notes.get("claude_unsafe", notes.get("unsafe")):
        notes["gemini_skipped"] = "Claude flagged it unsafe; not sent for a video check"
        db.update("clips", clip_id, ai_notes=json.dumps(notes))
        return False
    allowed, why = gemini.BREAKER.allow(cfg)
    if not allowed:
        log.info("clip %s: video check skipped (%s)", clip_id, why)
        notes["gemini_error"] = f"video check {why}"
        db.update("clips", clip_id, ai_notes=json.dumps(notes))
        return False
    ch = config_mod.channel(cfg, row["channel"]) or {"login": row["channel"]}
    spike = db.one("SELECT top_terms FROM spikes WHERE id = ?", (row["spike_id"],)) if row.get("spike_id") else None
    try:
        gem = gemini.review(cfg, Path(row["final_path"]),
                            streamer=ch.get("display_name") or row["channel"],
                            hook=(row.get("hook") or "").strip(), title=(row.get("title") or "").strip(),
                            transcript=row.get("transcript") or "",
                            chat_terms=(spike["top_terms"].split(",") if spike and spike["top_terms"]
                                        else [row["source_note"]] if row.get("source_note") else []))
    except Exception as exc:  # network, quota, policy, parse: keep Claude's verdict
        log.warning("clip %s: video check failed: %s", clip_id, exc)
        gemini.BREAKER.fail(cfg, exc)
        notes["gemini_error"] = str(exc)[:200]
        db.update("clips", clip_id, ai_notes=json.dumps(notes))
        return False
    gemini.BREAKER.ok()
    claude_q = int(notes.get("claude_quality", row.get("quality") or 0))
    claude_spoiler = notes.get("claude_spoiler", row.get("spoiler_risk") or "unknown")
    claude_unsafe = bool(notes.get("claude_unsafe", notes.get("unsafe", False)))
    g = cfg["gemini"]
    final = gemini.combine(claude_q, claude_spoiler, claude_unsafe, gem,
                           weight=float(g.get("weight", 0.6)), disagree_points=int(g.get("disagree_points", 3)))
    if final["disagree"] and cfg["twitch_clips"].get("tiebreak", True):
        from . import twitch_clips

        final = gemini.tiebreak(final, claude_q, gem["quality"], twitch_clips.evidence(cfg, row),
                                twitch_clips.evidence_text(row), name=gemini.label(gem))
    notes.update({"claude_quality": claude_q, "claude_spoiler": claude_spoiler, "claude_unsafe": claude_unsafe,
                  "gemini": gem, "disagree": final["disagree"], "unsafe": final["unsafe"]})
    notes.pop("gemini_error", None)
    notes.pop("gemini_skipped", None)
    notes.pop("tiebreak", None)
    if final.get("tiebreak"):
        notes["tiebreak"] = final["tiebreak"]
    db.update("clips", clip_id, quality=final["quality"], spoiler_risk=final["spoiler"],
              ai_notes=json.dumps(notes))
    log.info("clip %s: Claude %s + Gemini %s -> %s (spoiler %s%s)", clip_id, claude_q, gem["quality"],
             final["quality"], final["spoiler"],
             ", models disagree" if final["disagree"] else f", {final['tiebreak']}" if final.get("tiebreak") else "")
    return True


def drop_stale(db: DB, cfg: dict, now: float | None = None) -> int:
    """Queued clips that waited too long are dropped instead of rendered: the queue renders the
    best-scored first, so low scorers can wait for hours, and launch-week moments go stale."""
    minutes = float(cfg["clip"].get("max_queue_minutes", 0))
    if not minutes:
        return 0
    now = now or time.time()
    return db.execute(
        "UPDATE clips SET status = 'rejected', decided_by = 'auto', decided_at = ?, updated_at = ?, "
        "decision_note = ? WHERE status = 'queued' AND created_at < ?",
        (now, now, f"Stale: waited over {minutes:g} min to render while better clips went first",
         now - minutes * 60)).rowcount


class RenderWorker(threading.Thread):
    def __init__(self, db: DB, cfg: dict, stop: threading.Event, idx: int = 0):
        super().__init__(daemon=True, name=f"render-{idx}")
        self.db, self.cfg, self.stop_event = db, cfg, stop

    def run(self) -> None:
        while not self.stop_event.is_set():
            if dropped := drop_stale(self.db, self.cfg):
                log.info("dropped %s stale queued clip(s)", dropped)
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
