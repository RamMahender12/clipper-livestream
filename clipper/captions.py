"""Word-level captions with whisper.cpp, rendered as an ASS subtitle file.

The ASS file also carries the on-screen hook (first few seconds) and the
credit line, so one libass pass burns all text into the video.
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import tools

_NOISE = re.compile(r"^\s*[\[\(].*[\]\)]\s*$")  # [Music], (laughs)
_MUSIC = re.compile(r"music|singing|song|humming|melody|♪", re.I)


@dataclass
class Word:
    start: float
    end: float
    text: str


def _whisper_json(media: Path, model: Path, start: float = 0.0, duration: float | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        cmd = [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}"]
        if duration:
            cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-i", str(media), "-vn", "-ac", "1", "-ar", "16000", str(wav)]
        tools.run(cmd, timeout=120)
        base = Path(tmp) / "out"
        wcmd = [
            tools.whisper_cli(), "-m", str(model), "-f", str(wav),
            "-ml", "1", "-sow", "-oj", "-of", str(base), "-np", "-nt",
        ]
        dtw = _dtw_preset(model)
        if dtw:
            wcmd += ["-dtw", dtw]
        tools.run(wcmd, timeout=600)
        data = json.loads((base.with_suffix(".json")).read_text(encoding="utf-8", errors="replace"))
    return data


def transcribe(media: Path, model: Path, start: float = 0.0, duration: float | None = None) -> list[Word]:
    """Transcribe [start, start+duration] of `media`; word times are relative to `start`."""
    return words_from_whisper(_whisper_json(media, model, start, duration))


def transcribe_full(media: Path, model: Path, start: float = 0.0,
                    duration: float | None = None) -> tuple[list[Word], list[tuple[float, float]]]:
    """(speech words, music spans): whisper marks stretches of music like [MUSIC PLAYING]."""
    return split_markers(_raw_words(_whisper_json(media, model, start, duration)))


def _dtw_preset(model: Path) -> str | None:
    m = re.search(r"ggml-(tiny|base|small|medium|large-v3-turbo|large-v3|large-v2|large-v1)(\.en)?\.bin$", model.name)
    return f"{m.group(1)}{m.group(2) or ''}" if m else None


def _raw_words(data: dict) -> list[Word]:
    """Every token whisper produced, sound markers included; stray punctuation joins the word before."""
    words: list[Word] = []
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        off = seg.get("offsets") or {}
        start, end = off.get("from", 0) / 1000.0, off.get("to", 0) / 1000.0
        if words and not text[0].isalnum() and text[0] not in "[(♪" and len(text) <= 2:
            words[-1].text += text  # stray punctuation token
            words[-1].end = max(words[-1].end, end)
            continue
        words.append(Word(start, max(end, start + 0.05), text))
    return words


def split_markers(words: list[Word], merge_gap: float = 1.5) -> tuple[list[Word], list[tuple[float, float]]]:
    """Separate whisper's sound markers ([MUSIC PLAYING], (crickets chirping), ♪) from speech.

    With word-level timing one marker can arrive as several tokens ("[MUSIC", "PLAYING]"), which
    used to end up burned into the captions. Returns (speech words, merged music spans)."""
    speech: list[Word] = []
    music: list[tuple[float, float]] = []
    group: list[Word] = []
    for w in words:
        t = w.text.strip()
        if not group and t[:1] in "[(":
            group = [w]
        elif group:
            group.append(w)
        else:
            if "♪" in t:
                music.append((w.start, w.end))
                t = t.replace("♪", "").strip()
                if not t:
                    continue
                w = Word(w.start, w.end, t)
            speech.append(w)
            continue
        text = " ".join(g.text for g in group)
        if text.rstrip(".,!?;:")[-1:] in "])":
            if _MUSIC.search(text):
                music.append((group[0].start, group[-1].end))
            group = []
        elif len(group) > 6:  # never closed: it was speech after all, minus the bracket
            speech.extend(group[1:])
            group = []
    if group and _MUSIC.search(" ".join(g.text for g in group)):
        music.append((group[0].start, group[-1].end))
    merged: list[list[float]] = []
    for a, b in sorted(music):
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return speech, [(round(a, 2), round(b, 2)) for a, b in merged]


def words_from_whisper(data: dict) -> list[Word]:
    return split_markers(_raw_words(data))[0]


def transcript_text(words: list[Word]) -> str:
    return " ".join(w.text for w in words).strip()


def chunk_words(words: list[Word], per_chunk: int = 3, max_gap: float = 0.7) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= per_chunk or w.start - cur[-1].end > max_gap):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w.text[-1:] in ".?!":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _ts(sec: float) -> str:
    sec = max(0.0, sec)
    cs = int(round(sec * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# Swearing that gets Shorts age-restricted or demonetised when it's written on screen (and
# muted in the audio). Slurs are handled harder elsewhere: Claude/Gemini mark them unsafe.
PROFANE = re.compile(
    r"^(motherfuck\w*|f+u+c+k+\w*|shit|shits|shitty|shitting|shitted|bullshit|bitch\w*|cunts?|dicks?|dickhead\w*|"
    r"pussy|pussies|cocks?|cocksuck\w*|asshole\w*|whores?|sluts?|bastards?|nigg\w*|fags?|faggot\w*|retards?|retarded)$",
    re.I)


def is_profane(text: str) -> bool:
    core = re.sub(r"[^a-z]", "", text.lower())
    return bool(core) and bool(PROFANE.match(core))


def mask(text: str) -> str:
    """'fucking' -> 'f*****g': readable as a reaction, not as the word."""
    def one(m: re.Match) -> str:
        w = m.group(0)
        if not is_profane(w):
            return w
        return w[0] + "*" * max(1, len(w) - 2) + (w[-1] if len(w) > 2 else "")
    return re.sub(r"[A-Za-z']+", one, text)


def profane_spans(words: list["Word"], pad: float = 0.04) -> list[tuple[float, float]]:
    """(start, end) of every swear word, for muting the audio."""
    return [(max(0.0, w.start - pad), w.end + pad) for w in words if is_profane(w.text)]


def _clean(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(
    words: list[Word],
    *,
    width: int = 1080,
    height: int = 1920,
    font: str = "Arial Black",
    font_size: int = 84,
    highlight: str = "&H0000E5FF",
    per_chunk: int = 3,
    hook: str = "",
    hook_seconds: float = 3.0,
    credit: str = "",
    duration: float = 60.0,
    shift: float = 0.0,
    censor: bool = True,
) -> str:
    """`shift` moves every word time (use when the video was trimmed after transcription)."""
    words, _ = split_markers(words)  # [MUSIC PLAYING] and friends never go on screen
    cap_margin = int(height * 0.245)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
        f"-1,0,0,0,100,100,0,0,1,7,3,2,60,60,{cap_margin},1",
        f"Style: Hook,{font},{int(font_size * 0.95)},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,"
        f"-1,0,0,0,100,100,0,0,3,18,0,8,70,70,{int(height * 0.12)},1",
        f"Style: Credit,Arial,{int(font_size * 0.45)},&H40FFFFFF,&H40FFFFFF,&H80000000,&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,3,0,8,40,40,{int(height * 0.05)},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if credit:
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(duration)},Credit,,0,0,0,,{_clean(credit)}")
    if hook:
        lines.append(
            f"Dialogue: 3,{_ts(0)},{_ts(hook_seconds)},Hook,,0,0,0,,{{\\fad(0,250)}}{_clean((mask(hook) if censor else hook).upper())}"
        )
    chunks = chunk_words(words, per_chunk)
    for ci, chunk in enumerate(chunks):
        chunk_end = chunk[-1].end
        if ci + 1 < len(chunks) and chunks[ci + 1][0].start - chunk_end < 0.4:
            chunk_end = chunks[ci + 1][0].start
        for wi, w in enumerate(chunk):
            start = w.start
            end = chunk[wi + 1].start if wi + 1 < len(chunk) else chunk_end
            start, end = start + shift, end + shift
            if end <= 0 or start >= duration:
                continue
            parts = []
            for wj, other in enumerate(chunk):
                t = _clean((mask(other.text) if censor else other.text).upper())
                parts.append(f"{{\\c{highlight}}}{t}{{\\c&H00FFFFFF}}" if wj == wi else t)
            pop = "{\\fscx108\\fscy108\\t(0,80,\\fscx100\\fscy100)}" if wi == 0 else ""
            lines.append(
                f"Dialogue: 1,{_ts(start)},{_ts(min(end, duration))},Cap,,0,0,0,,{pop}{' '.join(parts)}"
            )
    return "\n".join(lines) + "\n"
