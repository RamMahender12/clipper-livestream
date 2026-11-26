"""Lucia campaign (Fanvue / creatorinseconds.com, Content Rewards): promo Reels built from the
brand's own AI Lucia clips. Not stream clipping: there's no streamer, the brand supplies the footage.

Every video follows the brief (Notion "GTA VI Lucia vs Fanvue AI Lucia", idea 1 "leaked GTA 6 footage"):
8-20 s, an on-screen AI line, says she's AI before the end, and the creatorinseconds end card
screen-blended over the last 3 s. Videos land as 'ready' clips of channel `ai_lucia` under campaign
`lucia`, so they get their own campaign tab on the dashboard.
"""
from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from . import config as config_mod, tools
from .db import DB

log = logging.getLogger(__name__)

CHANNEL, CAMPAIGN = "ai_lucia", "lucia"
AI_FOLDER = "1OOGMZip3lCvF1YgOCsen7Sag1GPfwn1S"  # brand's AI Lucia clips (public Drive folder)
END_CARD = "1bQSDdIWzF6LwgP9jjPJaD2CG_DpATI_Q"  # creatorinseconds_end_card_BLACK (use Screen blend)
W, H, FPS = 1080, 1920, 30
FIRST, SECOND = 4.5, 7.5  # seconds of each AI clip: 12 s total, inside the brief's 8-20 s

# (opening line over clip 1, reveal over clip 2, call to action over the end card).
# Every set carries the AI line and says she's AI before the video ends.
HOOKS = [
    ("leaked GTA 6 footage of Lucia is actually insane", "wait... this isn't GTA 6. she's AI",
     "you can make your own in seconds"),
    ("Rockstar really nailed Lucia in GTA 6", "plot twist: she's not from GTA 6. she's AI",
     "you can make your own AI girl in seconds"),
    ("GTA 6 Lucia in real life??", "she's not real. she's an AI creator", "make your own AI Lucia in seconds"),
    ("POV: you found Lucia outside of GTA 6", "she's AI btw", "you can make your own AI Lucia in seconds"),
    ("this GTA 6 Lucia cosplay is way too real", "because it's not cosplay. she's AI",
     "you can make your own AI creator in seconds"),
]
# Pin this on Instagram / post it first on TikTok (brief: "Comment CTA - Required").
COMMENTS = [
    "she's AI btw. you can make your own on creatorinseconds.com",
    "i literally made my own ai lucia in like 10 seconds 😭 creatorinseconds.com",
]
CAPTION = "she's AI 🤖 not the real Lucia. make your own in seconds: link in bio"
HASHTAGS = "ad,gta6,lucia,gtavi,aigirl"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def list_folder(html: str) -> list[tuple[str, str]]:
    """(file id, name) of the .mp4 files on a public Drive folder's embedded view."""
    pat = r'href="https://drive\.google\.com/file/d/([\w-]+)/view[^"]*".*?flip-entry-title">([^<]+)<'
    return [(fid, name) for fid, name in re.findall(pat, html, re.S) if name.lower().endswith(".mp4")]


def _download(fid: str, dest: Path) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = _get(f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t")
        if data[4:8] != b"ftyp":
            raise RuntimeError(f"Drive file {fid} didn't download as an mp4")
        dest.write_bytes(data)
    return dest


def pick_pair(files: list[str], used: dict[str, int], rng: random.Random) -> tuple[str, str]:
    """Two different clips, the least-used first, so the feed doesn't repeat itself."""
    order = sorted(files, key=lambda f: (used.get(f, 0), rng.random()))
    return order[0], order[1]


def _probe(path: Path) -> tuple[float, bool]:
    out = subprocess.run([tools.ffprobe(), "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    return float(info["format"]["duration"]), any(s["codec_type"] == "audio" for s in info.get("streams", []))


def _ass(lines: list[tuple[float, float, str, str]]) -> str:
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
            # opaque black box: many brand clips have their own TikTok caption burned in mid-frame
            "Style: T,DejaVu Sans,60,&H00FFFFFF,&H00000000,1,3,14,0,5,90,90,0\n"
            "Style: HUD,DejaVu Sans,58,&H0055D86A,&H00000000,1,1,4,0,9,0,50,120\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Text\n")

    def ts(s: float) -> str:
        return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"

    return head + "".join(f"Dialogue: 0,{ts(a)},{ts(b)},{st},{t}\n" for a, b, st, t in lines)


def build(clip_a: Path, clip_b: Path, end_card: Path, hook: tuple[str, str, str], out: Path) -> float:
    """Render one Reel; returns its length in seconds."""
    (da, aa), (db_, ab) = _probe(clip_a), _probe(clip_b)
    a, b = min(FIRST, da), min(SECOND, db_)
    total = a + b
    mid = f"{{\\pos({W // 2},{int(H * 0.30)})}}"
    ass = out.with_suffix(".ass")
    ass.write_text(_ass([
        (0, a, "HUD", "★★★☆☆\\N$1,250,000"),  # fake GTA HUD sells the "leaked footage" opener
        (0, a, "T", mid + hook[0]),
        (a, total - 3, "T", mid + hook[1]),
        (total - 3, total, "T", f"{{\\pos({W // 2},{int(H * 0.24)})}}" + hook[2]),
    ]), encoding="utf-8")
    fit = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,format=yuv420p"
    sub = str(ass).replace("\\", "/").replace(":", "\\:")
    fc = (f"[0:v]trim=0:{a},setpts=PTS-STARTPTS,{fit}[v0];[1:v]trim=0:{b},setpts=PTS-STARTPTS,{fit}[v1];"
          f"[v0][v1]concat=n=2:v=1:a=0,subtitles='{sub}'[v];"
          f"[v]split[p][q];[p]trim=0:{total - 3},setpts=PTS-STARTPTS[pre];"
          f"[q]trim={total - 3}:{total},setpts=PTS-STARTPTS,format=gbrp[tail];"
          # end card sits low, under her face (brief: don't cover her face); Screen blend drops its black background
          f"[2:v]scale={W}:{H},crop={W}:{H - int(H * 0.28)}:0:0,pad={W}:{H}:0:{int(H * 0.28)}:black,fps={FPS},trim=0:3,setpts=PTS-STARTPTS,"
          f"format=gbrp[card];[tail][card]blend=all_mode=screen:shortest=1,format=yuv420p[end];"
          f"[pre][end]concat=n=2:v=1:a=0[vout]")
    args = [tools.ffmpeg(), "-v", "error", "-y", "-i", str(clip_a), "-i", str(clip_b), "-i", str(end_card)]
    if aa and ab:
        fc += (f";[0:a]atrim=0:{a},asetpts=PTS-STARTPTS[a0];[1:a]atrim=0:{b},asetpts=PTS-STARTPTS[a1];"
               "[a0][a1]concat=n=2:v=0:a=1[aout]")
    else:  # a clip without sound: keep the track silent rather than out of sync
        args += ["-f", "lavfi", "-t", str(total), "-i", "anullsrc=r=44100:cl=stereo"]
    args += ["-filter_complex", fc, "-map", "[vout]", "-map", "[aout]" if aa and ab else "3:a",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "128k",
             "-t", str(total), "-movflags", "+faststart", str(out)]
    subprocess.run(args, check=True, capture_output=True)
    subprocess.run([tools.ffmpeg(), "-v", "error", "-y", "-ss", "1", "-i", str(out), "-frames:v", "1",
                    "-vf", "scale=360:-2", str(out.with_name("thumb.jpg"))], check=True, capture_output=True)
    ass.unlink(missing_ok=True)
    return total


def make(db: DB, cfg: dict, count: int = 1, rng: random.Random | None = None) -> list[int]:
    """Build `count` Reels and queue them for review under the lucia campaign. Returns clip ids."""
    rng = rng or random.Random()
    assets = config_mod.data_dir(cfg) / "lucia"
    files = dict((name, fid) for fid, name in list_folder(
        _get(f"https://drive.google.com/embeddedfolderview?id={AI_FOLDER}").decode("utf-8", "replace")))
    if len(files) < 2:
        raise RuntimeError("the AI Lucia Drive folder lists fewer than 2 clips")
    end_card = _download(END_CARD, assets / "end_card_black.mp4")
    used: dict[str, int] = {}
    for r in db.query("SELECT source FROM clips WHERE channel = ? AND source LIKE 'lucia:%'", (CHANNEL,)):
        for name in r["source"][len("lucia:"):].split("|"):
            used[name] = used.get(name, 0) + 1
    from .pipeline import clips_dir

    ids = []
    made = sum(used.values()) // 2  # videos built so far: hooks and comments rotate from here
    for i in range(count):
        a, b = pick_pair(sorted(files), used, rng)
        used[a], used[b] = used.get(a, 0) + 1, used.get(b, 0) + 1
        clip_a = _download(files[a], assets / "ai" / f"{files[a]}.mp4")
        clip_b = _download(files[b], assets / "ai" / f"{files[b]}.mp4")
        hook, comment, now = HOOKS[(made + i) % len(HOOKS)], COMMENTS[(made + i) % len(COMMENTS)], time.time()
        cid = db.insert("clips", channel=CHANNEL, campaign_id=CAMPAIGN, source=f"lucia:{a}|{b}",
                        wall_start=now, start_s=0, duration_s=0, status="rendering", hook=hook[0],
                        title=f"PIN THIS COMMENT: {comment}", caption=CAPTION, hashtags=HASHTAGS,
                        spoiler_risk="none", source_note=f"Lucia promo from the brand's AI clips: {a} + {b}",
                        created_at=now, updated_at=now)
        out = clips_dir(cfg) / f"{cid:05d}" / "final.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            length = build(clip_a, clip_b, end_card, hook, out)
        except subprocess.CalledProcessError as e:
            db.update("clips", cid, status="failed", error=(e.stderr or b"").decode(errors="replace")[-500:])
            raise
        db.update("clips", cid, status="ready", final_path=str(out), duration_s=length)
        ids.append(cid)
    return ids


def top_up(db: DB, cfg: dict) -> list[int]:
    """Keep [[campaigns]] lucia's keep_ready videos waiting for review (hourly, from housekeeping)."""
    camp = config_mod.campaign(cfg, CAMPAIGN)
    want = int((camp or {}).get("keep_ready", 0)) if camp and camp.get("active", True) else 0
    have = db.one("SELECT COUNT(*) AS n FROM clips WHERE campaign_id = ? AND status = 'ready'", (CAMPAIGN,))["n"]
    return make(db, cfg, want - have) if want > have else []
