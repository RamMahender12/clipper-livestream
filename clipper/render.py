"""Render a vertical 9:16 short from a horizontal stream recording."""
from __future__ import annotations

from pathlib import Path

from . import tools


def _escape_filter_path(p: Path) -> str:
    s = str(p)
    for a, b in (("\\", "\\\\"), (":", "\\:"), ("'", "\\'"), (",", "\\,"), ("[", "\\["), ("]", "\\]")):
        s = s.replace(a, b)
    return s


def build_filtergraph(
    layout: str,
    width: int = 1080,
    height: int = 1920,
    fps: int = 60,
    facecam: list[float] | None = None,
    ass_path: Path | None = None,
    zoom: float = 1.0,
) -> str:
    w, h = width, height
    zw = max(w, int(w * zoom) // 2 * 2)
    if layout == "facecam_split" and facecam:
        fx, fy, fw, fh = facecam
        cam_h = int(h / 3) // 2 * 2
        game_h = h - cam_h
        graph = (
            f"[0:v]fps={fps},split=2[a][b];"
            f"[a]crop=iw*{fw}:ih*{fh}:iw*{fx}:ih*{fy},"
            f"scale={w}:{cam_h}:force_original_aspect_ratio=increase,crop={w}:{cam_h}[cam];"
            f"[b]crop='min(iw,ih*{w}/{game_h})':ih,scale={w}:{game_h}:flags=lanczos[game];"
            f"[cam][game]vstack=inputs=2[base]"
        )
    elif layout == "crop":
        graph = f"[0:v]fps={fps},crop='min(iw,ih*{w}/{h})':ih,scale={w}:{h}:flags=lanczos[base]"
    else:  # blur: full frame in the middle, blurred copy fills the background
        graph = (
            f"[0:v]fps={fps},split=2[bg][fg];"
            f"[bg]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,crop={w // 4}:{h // 4},"
            f"gblur=sigma=12,eq=brightness=-0.12:saturation=1.15,scale={w}:{h}[bgb];"
            f"[fg]scale={zw}:-2:flags=lanczos,crop={w}:ih[fgs];"  # zoom > 1 trims the sides
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[base]"
        )
    if ass_path:
        graph += f";[base]ass=filename={_escape_filter_path(ass_path)}[v]"
    else:
        graph += ";[base]null[v]"
    return graph


def render(
    src: Path,
    out: Path,
    start: float,
    duration: float,
    *,
    layout: str = "blur",
    width: int = 1080,
    height: int = 1920,
    fps: int = 60,
    encoder: str = "libx264",
    facecam: list[float] | None = None,
    ass_path: Path | None = None,
    zoom: float = 1.0,
) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    graph = build_filtergraph(layout, width, height, fps, facecam, ass_path, zoom)
    if encoder == "h264_videotoolbox" and tools.has_encoder("h264_videotoolbox"):
        venc = ["-c:v", "h264_videotoolbox", "-b:v", "10M", "-maxrate", "14M", "-bufsize", "20M"]
    else:
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high"]
    cmd = [
        tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}", "-i", str(src),
        "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
        *venc, "-pix_fmt", "yuv420p",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(out),
    ]
    tools.run(cmd, timeout=900)
    return out


def thumbnail(video: Path, out: Path, at: float = 1.0) -> Path:
    tools.run([
        tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}",
        "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", str(out),
    ], timeout=60)
    return out
