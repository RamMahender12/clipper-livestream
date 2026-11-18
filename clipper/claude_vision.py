"""Video check on Claude: frames sampled from the finished clip, plus its transcript.

Same questions and the same answer shape as the Gemini reviewer (gemini.FIELDS / gemini.normalize),
so the rest of the pipeline (combine, tiebreak, hook swap, auto-approve gates) doesn't change.
Claude can't hear the clip: the transcript stands in for the sound. The first frames show the
burned-in hook, so "does the hook match" is judged from the picture.
"""
from __future__ import annotations

import base64
import copy
import tempfile
from pathlib import Path

from . import ai, tools
from .gemini import CONTEXT, FIELDS, GeminiError, SCHEMA, normalize

SYSTEM = ("You review vertical shorts cut from live Twitch streams of the video game GTA 6 during its launch "
          "week, before they are posted to a clip page on YouTube Shorts and Instagram Reels. You get frames "
          "sampled evenly through the finished clip, with their timestamps, and the auto-generated transcript. "
          "You can't hear the clip: judge what is said from the transcript. Answer every field honestly; when "
          "the frames don't show enough to be sure, say so in reason and lean cautious on the risk fields.")


def schema() -> dict:
    """Gemini's schema, minus the numeric bounds Claude's structured outputs don't take."""
    s = copy.deepcopy(SCHEMA)
    for prop in s["properties"].values():
        prop.pop("minimum", None)
        prop.pop("maximum", None)
        if prop.get("type") == "array" and prop.get("items", {}).get("type") == "object":
            prop["items"]["additionalProperties"] = False
    s["additionalProperties"] = False
    return s


def frame_times(duration: float, n: int) -> list[float]:
    """n timestamps spread through the clip; the first lands inside the hook's 3 seconds."""
    n = max(2, n)
    duration = max(duration, 1.0)
    return [round(min(duration - 0.1, 0.8 + i * (duration - 1.0) / (n - 1)), 2) for i in range(n)]


def sample_frames(video: Path, n: int = 8, width: int = 432) -> list[tuple[float, bytes]]:
    out: list[tuple[float, bytes]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(frame_times(tools.duration(video), n)):
            jpg = Path(tmp) / f"f{i}.jpg"
            tools.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}",
                       "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(jpg)],
                      timeout=60)
            if jpg.exists():
                out.append((t, jpg.read_bytes()))
    if not out:
        raise GeminiError("couldn't read frames from the clip")
    return out


def build_content(frames: list[tuple[float, bytes]], prompt: str) -> list[dict]:
    content: list[dict] = []
    for t, jpg in frames:
        content.append({"type": "text", "text": f"Frame at {t:.1f}s"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(jpg).decode()}})
    content.append({"type": "text", "text": prompt})
    return content


def review(cfg: dict, video: Path, *, streamer: str, hook: str, transcript: str, chat_terms: list[str],
           title: str = "") -> dict:
    g = cfg["gemini"]
    frames = sample_frames(video, int(g.get("frames", 8)), int(g.get("frame_width", 432)))
    prompt = ("Answer these about the clip:\n\n" + FIELDS + "\n\n" +
              CONTEXT.format(streamer=streamer, hook=hook, title=title, chat=", ".join(chat_terms) or "n/a",
                             transcript=transcript[:1500] or "(no speech)"))
    data, model = ai.call_json(cfg, SYSTEM, build_content(frames, prompt), schema(),
                               effort=g.get("claude_effort", "low"),
                               model=g.get("claude_model", "claude-sonnet-5-5"), max_tokens=4000,
                               purpose="video_check", thinking=g.get("claude_thinking", "off"))
    if data is None:
        raise GeminiError(f"Claude video check failed: {model}")
    return normalize(data, model)
