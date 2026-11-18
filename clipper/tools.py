"""Locate external binaries (ffmpeg with libass, whisper.cpp, streamlink)."""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path

FFMPEG_CANDIDATES = [
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",  # brew ffmpeg-full has libass (captions)
    "/usr/local/opt/ffmpeg-full/bin/ffmpeg",
    "ffmpeg",
]


class ToolMissing(RuntimeError):
    pass


def _which(name: str) -> str | None:
    p = Path(name)
    if p.is_absolute():
        return str(p) if p.exists() else None
    return shutil.which(name)


@functools.cache
def ffmpeg() -> str:
    """Prefer an ffmpeg build that has the `ass` filter (needed to burn captions)."""
    fallback = None
    for cand in FFMPEG_CANDIDATES:
        path = _which(cand)
        if not path:
            continue
        fallback = fallback or path
        if has_filter(path, "ass"):
            return path
    if fallback:
        return fallback
    raise ToolMissing("ffmpeg not found. Install: brew install ffmpeg-full")


@functools.cache
def ffprobe() -> str:
    ff = Path(ffmpeg())
    sibling = ff.with_name("ffprobe")
    if sibling.exists():
        return str(sibling)
    path = shutil.which("ffprobe")
    if not path:
        raise ToolMissing("ffprobe not found")
    return path


@functools.cache
def has_filter(ffmpeg_path: str, name: str) -> bool:
    try:
        out = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


@functools.cache
def has_encoder(name: str) -> bool:
    out = subprocess.run(
        [ffmpeg(), "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20
    ).stdout
    return f" {name} " in out


def whisper_cli() -> str:
    for name in ("whisper-cli", "whisper-cpp"):
        path = shutil.which(name)
        if path:
            return path
    raise ToolMissing("whisper.cpp not found. Install: brew install whisper-cpp")


def streamlink() -> str:
    path = shutil.which("streamlink")
    if not path:
        raise ToolMissing("streamlink not found. Install: brew install streamlink")
    return path


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({proc.returncode}): {tail}")
    return proc


def probe(path: str | Path) -> dict:
    out = run([
        ffprobe(), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], timeout=60).stdout
    return json.loads(out)


def duration(path: str | Path) -> float:
    info = probe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def video_size(path: str | Path) -> tuple[int, int]:
    for s in probe(path).get("streams", []):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError(f"no video stream in {path}")
