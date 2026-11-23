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
    from . import ai

    ai.set_channel(clip["channel"])  # Claude spend in this clip's work counts for its streamer
    clip_id = clip["id"]
    ch = config_mod.channel(cfg, clip["channel"]) or {"login": clip["channel"]}
    camp = guardrails.campaign_for(cfg, ch)
    src = Path(clip["source"])
    if not src.exists():
        raise FileNotFoundError(f"source missing: {src}")
    start, dur = float(clip["start_s"] or 0), float(clip["duration_s"] or 30)

    # 0. one frame, Haiku: is this GTA 6 at all? Giveaways, IRL and other games stop here, before
    # transcription, packaging, the render and the full video check
    if why := quick_game_check(db, cfg, clip, src, start, dur):
        _finish(db, clip_id, status="rejected", decided_by="auto", decided_at=time.time(),
                decision_note=f"Dropped: {why}"[:1500])
        log.info("clip %s dropped by the quick game check", clip_id)
        return

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
        notes = {"take": meta.take, "why": meta.why, "unsafe": meta.unsafe, "source": meta.source,
                 # Claude's own verdict, kept apart from the combined one
                 "claude_quality": meta.quality, "claude_spoiler": meta.spoiler_risk,
                 "claude_unsafe": meta.unsafe}
        if meta.source == "template" and meta.ai_error:
            # Claude was down or over its cap: redo_after_outage writes it properly once it's back
            notes.update(packaging_error=meta.ai_error, template_hook=meta.hook)
        prev = _notes(db.one("SELECT ai_notes FROM clips WHERE id = ?", (clip_id,)) or clip)
        notes.update({k: prev[k] for k in ("ai_redos", "ai_redo_at", "precheck") if k in prev})
        db.update(
            "clips", clip_id,
            hook=meta.hook, title=meta.title, caption=meta.caption,
            hashtags=",".join(meta.hashtags), spoiler_risk=meta.spoiler_risk,
            quality=meta.quality, ai_notes=json.dumps(notes),
        )
        hook = meta.hook
        # a weak chat-spike moment stops here: no render, video check or judge. Claude's transcript
        # score of 4 or below never became a postable chat clip in launch-day data (0 of 38).
        # Viewer clips always go on: Claude underrates visual moments it can only read about.
        floor = int(cfg["ai"].get("min_quality_to_render", 0))
        from . import twitch_clips

        strong_viewer = not clip.get("spike_id") and twitch_clips.evidence(cfg, clip) == "strong"
        if meta.source != "template" and meta.quality < floor and not strong_viewer:
            _finish(db, clip_id, status="rejected", decided_by="auto", decided_at=time.time(),
                    decision_note=f"Weak moment: Claude rated it {meta.quality}/10 from the transcript "
                                  f"(needs {floor} to be rendered and reviewed)")
            log.info("clip %s: skipped, Claude rated it %s/10", clip_id, meta.quality)
            return
    else:
        hook = clip["hook"].strip()  # " " means a human cleared it on purpose

    # 3. captions + render, then Gemini watches the result
    render_final(db, cfg, clip_id, ch, src, start, dur, words, hook)
    # our own hook (never one you wrote) gets fixed if the video check says it misleads
    check_and_finish(db, cfg, clip_id, own_hook=not clip["hook"])


def _notes(row) -> dict:
    try:
        return json.loads(dict(row).get("ai_notes") or "{}")
    except json.JSONDecodeError:
        return {}


def quick_game_check(db: DB, cfg: dict, clip: dict, src: Path, start: float, dur: float) -> str:
    """Why the clip is dropped as not GTA 6 after Haiku looked at one frame from the middle of the
    moment, or "" to carry on. Runs once, while the video check is on, on clips we package ourselves
    (not a re-render with your hook, not one you decided on). Only "no game on screen" drops it.
    "Another game" doesn't: GTA 6 is newer than Haiku, which took real GTA 6 footage for other games
    (19 Nov, 245 clips Sonnet had judged: "no game" caught 109 of 122 non-GTA clips and wrongly
    flagged 4 of 123 GTA 6 ones, all rated 2-4/10; "another game" was wrong 10 times in 13). GTA 6,
    another game, unsure, or a check that couldn't run (Claude down or over its cap) all go on to the
    full video check. [gemini].precheck = false turns it off."""
    g = cfg.get("gemini", {})
    notes = _notes(clip)
    if (not g.get("enabled") or not g.get("precheck", True) or not g.get("reject_off_game", True) or clip.get("hook")
            or clip.get("decided_by") == "human" or "precheck" in notes):
        return ""
    from . import claude_vision

    try:
        shows, what, model = claude_vision.precheck(cfg, src, start + dur / 2)
    except Exception as exc:
        log.info("clip %s: quick game check skipped: %s", clip["id"], exc)
        return ""
    notes["precheck"] = {"shows": shows, "what": what[:200], "model": model}
    db.update("clips", clip["id"], ai_notes=json.dumps(notes))
    if shows != "not_a_game":
        return ""
    return f"not GTA 6 gameplay (quick check, no game on screen){': ' + what[:200] if what else ''}"


def _finish(db: DB, clip_id: int, **fields) -> bool:
    """Leave the rendering state, unless you decided on the clip while it rendered."""
    return db.update_if("clips", clip_id, {"status": "rendering"}, **fields)


def _to_drive(db: DB, cfg: dict, clip_id: int) -> None:
    """Put the finished clip in Google Drive and keep its link on the clip. Never fails the clip."""
    from pathlib import Path

    from .publishers import drive, export

    try:
        if not drive.enabled(cfg):
            return
        row = dict(db.one("SELECT final_path FROM clips WHERE id = ?", (clip_id,)))
        video = Path(row.get("final_path") or "")
        if video.is_file():
            link = drive.upload(cfg, video, export._today(cfg), name=f"{clip_id}_{video.name}")
            db.execute("UPDATE clips SET drive_url = ? WHERE id = ?", (link, clip_id))  # not db.update: keep updated_at
            log.info("clip %s in Drive: %s", clip_id, link)
    except Exception as exc:
        log.warning("clip %s: Drive upload failed: %s", clip_id, str(exc)[:200])


def check_and_finish(db: DB, cfg: dict, clip_id: int, own_hook: bool) -> None:
    """The steps after a render: video check, cut violence and music, sharper hook, then ready."""
    gemini_review(db, cfg, clip_id)  # second opinion on the finished video
    row = dict(db.one("SELECT decided_by, ai_notes FROM clips WHERE id = ?", (clip_id,)))
    if row.get("decided_by") != "human" and (why := guardrails.off_game(cfg, _notes(row))):
        # before any trim or hook swap: those re-render, and could pay for another check
        _finish(db, clip_id, status="rejected", decided_by="auto", decided_at=time.time(),
                decision_note=f"Dropped: {why}"[:1500])
        log.info("clip %s dropped: not GTA 6 gameplay", clip_id)
        return
    if cut_violence(db, cfg, clip_id) == "mostly":
        _finish(db, clip_id, status="rejected", decided_by="auto", decided_at=time.time(),
                decision_note="Dropped: mostly violence (YouTube age-restricts it), nothing clean enough to keep")
        log.info("clip %s dropped: mostly violence", clip_id)
        return
    if own_hook:
        swap_hook(db, cfg, clip_id)
    if _finish(db, clip_id, status="ready"):
        log.info("clip %s ready", clip_id)
        _to_drive(db, cfg, clip_id)
    else:
        log.info("clip %s: you decided on it while it rendered; leaving your decision", clip_id)


AI_REDOS = 3            # tries per clip at a step Claude/Gemini couldn't do
AI_REDO_GAP = 15 * 60   # seconds between tries


def _redo_step(cfg: dict, row: dict, notes: dict) -> str:
    """'package' or 'check' when that step fell through for a reason that wasn't the clip's fault."""
    from . import ai, gemini

    if notes.get("source", "template") == "template":
        own = (row.get("hook") or "") == notes.get("template_hook")  # not a hook you typed since
        return "package" if own and ai.temporary(notes.get("packaging_error", "")) else ""
    if notes.get("gemini") or not notes.get("gemini_error") or not gemini.configured(cfg):
        return ""
    if notes.get("claude_unsafe", notes.get("unsafe")):  # never sent for a video check on purpose
        return ""
    return "check"


def redo_after_outage(db: DB, cfg: dict, now: float | None = None) -> int | None:
    """When Claude was down or over its daily cap, a clip got template packaging or no video check,
    and the auto-approver can only hold it for you. Once AI is back, redo that step (best clip first,
    at most AI_REDOS times each), so the clip gets a real review instead of waiting forever. The
    render worker calls this when it has nothing queued, so fresh clips always go first.
    Returns the clip id it worked on, or None."""
    from . import ai, gemini

    now = now or time.time()
    up = ai.available(cfg)[0]
    rows = db.query(
        "SELECT * FROM clips WHERE status = 'ready' AND COALESCE(decided_by, '') != 'human' "
        "AND (ai_notes LIKE '%packaging_error%' OR ai_notes LIKE '%gemini_error%') "
        "ORDER BY score DESC, id ASC")
    for r in rows:
        clip = dict(r)
        notes = _notes(clip)
        step = _redo_step(cfg, clip, notes)
        tries = int(notes.get("ai_redos", 0))
        if not step or tries >= AI_REDOS or now - float(notes.get("ai_redo_at", 0)) < AI_REDO_GAP:
            continue
        if (step == "package" or cfg["gemini"].get("backend") == "claude") and not up:
            continue
        if step == "check" and gemini.BREAKER.paused():
            continue
        notes.update(ai_redos=tries + 1, ai_redo_at=now)
        if not db.update_if("clips", clip["id"], {"status": "ready", "updated_at": clip["updated_at"]},
                            status="rendering", ai_notes=json.dumps(notes)):
            continue  # you acted on it just now
        log.info("clip %s: AI is back, redoing the %s (try %d)", clip["id"],
                 "packaging" if step == "package" else "video check", tries + 1)
        try:
            if step == "package":
                clip.update(hook="", ai_notes=json.dumps(notes), status="rendering")
                process_clip(db, cfg, clip)
            else:
                check_and_finish(db, cfg, clip["id"], own_hook=True)
        except Exception as exc:  # keep the clip as it was, still reviewable by you
            log.warning("clip %s: redo failed: %s", clip["id"], exc)
            _finish(db, clip["id"], status="ready")
        return clip["id"]
    return None


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
        take = ""
        if capcfg.get("take_overlay", True):  # our own commentary on screen: not a bare repost
            row = db.one("SELECT ai_notes FROM clips WHERE id = ?", (clip_id,))
            try:
                take = str((json.loads(row["ai_notes"] or "{}") if row else {}).get("take") or "")
            except (json.JSONDecodeError, TypeError):
                take = ""
            limit = int(capcfg.get("take_max_chars", 90))
            if len(take) > limit:  # older takes ran long: end on a whole word
                take = take[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
        ass = captions.build_ass(
            words,
            width=ccfg["width"], height=ccfg["height"],
            font=capcfg["font"], font_size=capcfg["font_size"],
            highlight=capcfg["highlight_color"], per_chunk=capcfg["words_per_chunk"],
            hook=hook, hook_seconds=capcfg["hook_seconds"],
            credit=credit if ch.get("credit_overlay", True) else "", duration=dur,
            censor=capcfg.get("censor_profanity", True), take=take,
        )
        ass_path = out_dir / "captions.ass"
        ass_path.write_text(ass, encoding="utf-8")
    final = out_dir / "final.mp4"
    camp = guardrails.campaign_for(cfg, ch) or {}  # a campaign may require its logo on every clip
    logo = config_mod.resolve(cfg, camp["logo"]) if camp.get("logo") else None
    if logo and not logo.is_file():
        raise FileNotFoundError(f"campaign {camp.get('id')} logo missing: {logo}")  # never post without it
    render.render(
        src, final, start, dur,
        layout=ch.get("layout") or ccfg["layout"],
        width=ccfg["width"], height=ccfg["height"], fps=ccfg["fps"],
        encoder=ccfg["encoder"], facecam=ch.get("facecam"), ass_path=ass_path,
        zoom=float(ch.get("zoom", ccfg.get("zoom", 1.0))),
        mute=captions.profane_spans(words) if capcfg.get("mute_profanity", True) else None,
        logo=logo,
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
    if g.get("recheck_after_swap", False):
        gemini_review(db, cfg, clip_id)
    else:
        # Same footage, and the new hook is the video check's own words: take it as matching rather
        # than pay for a second check (19 Nov: ~$1.50 a day, and it rejected its own hook 48 times
        # in ~120). Everything else it found (violence, age, music, game) is about the footage.
        gem["hook_matches"] = True
        db.update("clips", clip_id, ai_notes=json.dumps(notes))
    log.info("clip %s: hook swapped to Gemini's %r (was %r)", clip_id, new, old)
    return True


def gemini_review(db: DB, cfg: dict, clip_id: int) -> bool:
    """Have Gemini watch the rendered clip and merge its verdict with Claude's.
    Never fails the clip: without Gemini, Claude's verdict stands."""
    from . import ai, gemini

    ai.set_channel((db.one("SELECT channel FROM clips WHERE id = ?", (clip_id,)) or {"channel": ""})["channel"])
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
        self.db, self.cfg, self.stop_event, self.idx = db, cfg, stop, idx

    def run(self) -> None:
        from . import retention

        last_redo = last_clean = 0.0
        while not self.stop_event.is_set():
            if dropped := drop_stale(self.db, self.cfg):
                log.info("dropped %s stale queued clip(s)", dropped)
            if self.idx == 0 and time.time() - last_clean > 3600:  # one worker keeps the disk tidy
                last_clean = time.time()
                try:
                    retention.housekeeping(self.db, self.cfg)
                except Exception:
                    log.exception("media cleanup failed")
            row = self.db.claim_clip("queued", "rendering")
            if not row:
                if time.time() - last_redo > 60:  # idle: catch up on clips AI couldn't review earlier
                    last_redo = time.time()
                    try:
                        if redo_after_outage(self.db, self.cfg):
                            continue
                    except Exception:
                        log.exception("redo after AI outage failed")
                self.stop_event.wait(2)
                continue
            try:
                process_clip(self.db, self.cfg, dict(row))
            except Exception as exc:
                log.exception("clip %s failed", row["id"])
                _finish(self.db, row["id"], status="failed", error=str(exc)[:1000])


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
            _finish(db, row["id"], status="failed", error=str(exc)[:1000])
