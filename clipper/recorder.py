"""Live-status checks and segmented recording via streamlink -> ffmpeg.

The recording is split into ~30 s .ts segments named by UTC wall-clock start
time. Chat timestamps use the same clock, so a chat spike at wall time T maps
straight to the segments that cover T, with no timestamp arithmetic on a
6-hour file. Old segments are deleted after `retain_minutes`.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import tools

log = logging.getLogger(__name__)

SEG_FMT = "%Y%m%d-%H%M%S"


@dataclass
class LiveInfo:
    live: bool
    title: str = ""
    category: str = ""
    error: str = ""


def check_live(login: str, timeout: float = 45) -> LiveInfo:
    try:
        proc = subprocess.run(
            [tools.streamlink(), "--json", f"twitch.tv/{login}"],
            capture_output=True, text=True, timeout=timeout,
        )
        data = json.loads(proc.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        return LiveInfo(False, error=str(exc))
    if data.get("error"):
        return LiveInfo(False, error=data["error"])
    meta = data.get("metadata") or {}
    return LiveInfo(bool(data.get("streams")), meta.get("title") or "", meta.get("category") or "")


def segment_start(path: Path) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(path.stem, SEG_FMT)))
    except ValueError:
        return None


def list_segments(seg_dir: Path) -> list[tuple[float, Path]]:
    out = []
    for p in seg_dir.glob("*.ts"):
        start = segment_start(p)
        if start is not None:
            out.append((start, p))
    out.sort()
    return out


def covering(seg_dir: Path, t0: float, t1: float) -> tuple[list[tuple[float, Path]], bool]:
    """Segments overlapping [t0, t1] and whether they are all closed (complete).

    A segment is closed once a later segment exists."""
    segs = list_segments(seg_dir)
    picked = []
    for i, (start, path) in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else float("inf")
        if start <= t1 and nxt > t0:
            picked.append((start, path))
    closed = bool(segs) and segs[-1][0] > t1
    return picked, closed


def extract_window(
    seg_dir: Path, t0: float, t1: float, out_path: Path, recording_done: bool = False
) -> float | None:
    """Concatenate the segments covering [t0, t1] into out_path (stream copy).

    Returns the offset of t0 inside out_path, or None if the segments aren't
    complete yet (try again later)."""
    picked, closed = covering(seg_dir, t0, t1)
    if not picked or not (closed or recording_done):
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # concat demuxer (not the concat: protocol) offsets each segment's
    # timestamps so the joined file plays continuously
    listing = out_path.with_suffix(".txt")
    def _quote(path: Path) -> str:  # concat-list quoting; paths resolve relative to the list file
        return "'" + str(path.resolve()).replace("'", "'\\''") + "'"

    listing.write_text("".join(f"file {_quote(p)}\n" for _, p in picked))
    try:
        tools.run([
            tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", str(out_path),
        ], timeout=300)
    finally:
        listing.unlink(missing_ok=True)
    return max(0.0, t0 - picked[0][0])


class Recorder:
    """Runs `streamlink --stdout | ffmpeg -f segment` for one live channel."""

    def __init__(self, login: str, seg_dir: Path, quality: str, segment_seconds: int):
        self.login = login
        self.seg_dir = seg_dir
        self.quality = quality
        self.segment_seconds = int(segment_seconds)
        self._sl: subprocess.Popen | None = None
        self._ff: subprocess.Popen | None = None

    def start(self) -> None:
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        sl_cmd = [
            tools.streamlink(), "--stdout", "--retry-open", "3",
            "--stream-segment-threads", "2", "--loglevel", "warning",
            f"twitch.tv/{self.login}", self.quality,
        ]
        ff_cmd = [
            tools.ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(self.segment_seconds),
            "-reset_timestamps", "1", "-strftime", "1",
            str(self.seg_dir / f"{SEG_FMT}.ts"),
        ]
        env = {**os.environ, "TZ": "UTC"}  # strftime names in UTC
        # stderr goes to files: an unread PIPE fills up and freezes the recording
        self._sl_log = open(self.seg_dir / "streamlink.log", "ab")
        self._ff_log = open(self.seg_dir / "ffmpeg.log", "ab")
        self._sl = subprocess.Popen(sl_cmd, stdout=subprocess.PIPE, stderr=self._sl_log)
        self._ff = subprocess.Popen(
            ff_cmd, stdin=self._sl.stdout, stderr=self._ff_log, env=env
        )
        self._sl.stdout.close()  # ffmpeg owns the pipe now
        log.info("recording %s -> %s", self.login, self.seg_dir)

    def alive(self) -> bool:
        return bool(self._ff and self._ff.poll() is None)

    def stop(self) -> None:
        for proc in (self._sl, self._ff):
            if proc and proc.poll() is None:
                proc.terminate()
        for proc in (self._sl, self._ff):
            if proc:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for fh in (getattr(self, "_sl_log", None), getattr(self, "_ff_log", None)):
            if fh:
                fh.close()

    def errors(self) -> str:
        msgs = []
        for name in ("streamlink", "ffmpeg"):
            path = self.seg_dir / f"{name}.log"
            try:
                tail = path.read_bytes()[-600:].decode("utf-8", "replace").strip()
            except OSError:
                tail = ""
            if tail:
                msgs.append(f"{name}: {tail}")
        return "\n".join(msgs)

    def last_write_age(self) -> float:
        """Seconds since the newest segment was written (stall detection)."""
        segs = list_segments(self.seg_dir)
        if not segs:
            return float("inf")
        try:
            return time.time() - segs[-1][1].stat().st_mtime
        except OSError:
            return float("inf")


def prune(seg_dir: Path, retain_seconds: float, now: float | None = None) -> int:
    now = now or time.time()
    removed = 0
    segs = list_segments(seg_dir)
    try:  # nothing written for longer than we keep segments: the session ended, its last two can go too
        ended = bool(segs) and now - segs[-1][1].stat().st_mtime > retain_seconds
    except OSError:
        ended = False
    for start, path in (segs if ended else segs[:-2]):  # never touch the newest (being written)
        if now - start > retain_seconds:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    if ended:
        try:
            seg_dir.rmdir()  # only if empty
        except OSError:
            pass
    return removed


def kill_orphans(rec_root: Path) -> int:
    """Stop ffmpeg recorders left behind by a previous run that died without
    cleaning up (their streamlink feeders exit once ffmpeg is gone)."""
    try:
        out = subprocess.run(["pgrep", "-f", str(rec_root)], capture_output=True, text=True).stdout
    except OSError:
        return 0
    killed = 0
    for tok in out.split():
        if not tok.isdigit() or int(tok) == os.getpid():
            continue
        pid = int(tok)
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout
        if "ffmpeg" not in comm:
            continue
        try:
            os.kill(pid, 15)
            killed += 1
        except OSError:
            pass
    return killed


class Pruner(threading.Thread):
    def __init__(self, root: Path, retain_seconds: float, stop: threading.Event):
        super().__init__(daemon=True, name="pruner")
        self.root, self.retain, self.stop_event = root, retain_seconds, stop

    def run(self) -> None:
        while not self.stop_event.wait(60):
            for d in self.root.glob("*/*"):
                if d.is_dir():
                    prune(d, self.retain)
