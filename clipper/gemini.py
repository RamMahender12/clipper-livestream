"""Second opinion from Gemini, which actually watches the rendered clip.

Claude judges a clip from its transcript and chat reaction; Gemini watches
the final video (picture and sound, hook included) and reports what happens
on screen, a visual quality score, spoiler/cutscene and safety verdicts, and
whether the hook matches. combine() merges both into the clip's final score:
weighted quality, the more cautious spoiler verdict, unsafe if either says so,
and a "disagree" flag when the two scores are far apart (held for a human).

Two backends (config [gemini] backend):
- "vertex" (default): Vertex AI on your Google Cloud project, authenticated
  with a service account that only has the Vertex AI User role. Gemini gets a
  light 540p preview copy inline (judges the same, uploads instantly). Usage
  bills to the project's billing account, so Cloud credits apply.
- "ai_studio": Gemini API key (GEMINI_API_KEY), Files API upload, then the
  Interactions API (generateContent as a fallback). Needs AI Studio prepay. Docs checked
18 Nov 2026: aistudio.google.com/docs/video-understanding, /docs/files,
/docs/structured-output, /api-reference/interactions-api.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com"
API_REVISION = "2026-05-20"
RISK_RANK = {"none": 0, "low": 1, "high": 2}

SCHEMA = {
    "type": "object",
    "properties": {
        "what_happens": {"type": "string"},
        "visual_quality": {"type": "integer", "minimum": 1, "maximum": 10},
        "spoiler_risk": {"type": "string", "enum": ["none", "low", "high"]},
        "is_cutscene": {"type": "boolean"},
        "unsafe": {"type": "boolean"},
        "hook_matches": {"type": "boolean"},
        "better_hook": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["what_happens", "visual_quality", "spoiler_risk", "is_cutscene", "unsafe",
                 "hook_matches", "better_hook", "reason"],
}

PROMPT = """You are reviewing a vertical short (about 30 seconds) cut from a live Twitch stream of the video game GTA 6 during its launch week. It will be posted to a clip page on YouTube Shorts and Instagram Reels. Watch the whole clip, picture and sound, and answer:

- what_happens: 1-2 sentences describing what is visible and audible.
- visual_quality: 1-10, how likely a stranger scrolling past stops and watches to the end. Reward clear action, chaos, funny or surprising moments, a payoff, and readable visuals. Score low for menus, loading screens, walking around with nothing happening, or talking over nothing.
- spoiler_risk: "high" if it shows a cutscene, a story mission's outcome, a character death or betrayal, the ending, or a plot reveal; "low" for early story context; "none" for free-roam gameplay, chaos and reactions.
- is_cutscene: true if the clip is mostly a non-interactive cinematic (letterbox bars, no game HUD, cinematic camera).
- unsafe: true for slurs, harassment of a real person, sexual content, or anything against YouTube or Instagram community guidelines.
- hook_matches: whether the on-screen hook text shown at the start matches what actually happens.
- better_hook: a stronger hook of at most 7 words if the current one doesn't match or is weak, otherwise "".
- reason: one sentence explaining your score.

Context: streamer {streamer}. On-screen hook: {hook!r}. Chat was spamming: {chat}. Transcript (auto-generated, may contain errors): {transcript!r}"""


class GeminiError(Exception):
    pass


def configured(cfg: dict) -> bool:
    g = cfg.get("gemini", {})
    if not g.get("enabled"):
        return False
    if g.get("backend", "vertex") == "vertex":
        from . import config as config_mod

        return config_mod.resolve(cfg, g.get("service_account_file", "secrets/vertex_sa.json")).exists()
    return bool(os.environ.get(g.get("api_key_env", "GEMINI_API_KEY")))


# ---------------------------------------------------------------- Vertex AI
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _sa_info(cfg: dict) -> tuple[Path, dict]:
    from . import config as config_mod

    path = config_mod.resolve(cfg, cfg["gemini"].get("service_account_file", "secrets/vertex_sa.json"))
    return path, json.loads(path.read_text())


def vertex_token(cfg: dict) -> str:
    """OAuth access token for the service account (signed JWT -> token), cached ~55 min."""
    path, info = _sa_info(cfg)
    cached = _TOKEN_CACHE.get(str(path))
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    from google.auth import crypt, jwt  # google-auth: only for RS256 signing

    now = int(time.time())
    token_uri = info.get("token_uri", "https://oauth2.googleapis.com/token")
    signer = crypt.RSASigner.from_service_account_info(info)
    assertion = jwt.encode(signer, {
        "iss": info["client_email"], "aud": token_uri, "iat": now, "exp": now + 3600,
        "scope": "https://www.googleapis.com/auth/cloud-platform"})
    if isinstance(assertion, bytes):
        assertion = assertion.decode()
    body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": assertion}).encode()
    req = urllib.request.Request(token_uri, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tok = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise GeminiError(f"service account sign-in failed: {_err(exc.code, exc.read())}") from None
    _TOKEN_CACHE[str(path)] = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)))
    return tok["access_token"]


def make_proxy(video: Path) -> Path:
    """Light preview copy for review: 540 px wide, 12 fps, small audio (~1-2 MB for 30 s)."""
    from . import tools

    out = video.with_name(video.stem + ".review.mp4")
    tools.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
               "-vf", "scale=540:-2,fps=12", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
               "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(out)], timeout=180)
    return out


def vertex_request_body(prompt: str, video_bytes: bytes, thinking_level: str = "low") -> dict:
    import base64

    return {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": "video/mp4", "data": base64.b64encode(video_bytes).decode()}},
            {"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": SCHEMA,
                             "thinkingConfig": {"thinkingLevel": thinking_level}},
    }


def ask_vertex(cfg: dict, proxy: Path, prompt: str) -> tuple[dict, str]:
    g = cfg["gemini"]
    _, info = _sa_info(cfg)
    project = g.get("vertex_project") or info.get("project_id", "")
    location = g.get("vertex_location", "global")
    model = g.get("model", "gemini-3.8-flash")
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    url = (f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/"
           f"{model}:generateContent")
    body = json.dumps(vertex_request_body(prompt, proxy.read_bytes(), g.get("thinking_level", "low"))).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {vertex_token(cfg)}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise GeminiError(f"Vertex AI {_err(exc.code, exc.read())}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise GeminiError(f"Vertex AI connection error: {exc}") from None
    text = generate_content_text(data)
    if not text:
        reason = ((data.get("candidates") or [{}])[0].get("finishReason")
                  or (data.get("promptFeedback") or {}).get("blockReason") or "empty answer")
        raise GeminiError(f"Vertex AI returned no text ({reason})")
    return _parse_json(text), data.get("modelVersion") or model


def _key(cfg: dict) -> str:
    return os.environ.get(cfg["gemini"].get("api_key_env", "GEMINI_API_KEY"), "")


def _request(method: str, url: str, key: str, body: bytes | None = None, headers: dict | None = None,
             timeout: float = 120):
    h = {"x-goog-api-key": key, **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _err(status: int, body: bytes) -> str:
    try:
        e = json.loads(body).get("error") or {}
        return f"HTTP {status}: {e.get('message') or e.get('status') or body[:200]!r}"[:300]
    except (ValueError, AttributeError):
        return f"HTTP {status}: {body[:200]!r}"


# ---------------------------------------------------------------- Files API
def upload(key: str, video: Path, timeout: float = 300) -> dict:
    size = video.stat().st_size
    status, headers, body = _request(
        "POST", f"{BASE}/upload/v1beta/files", key,
        body=json.dumps({"file": {"display_name": video.parent.name or video.stem}}).encode(),
        headers={"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(size),
                 "X-Goog-Upload-Header-Content-Type": "video/mp4", "Content-Type": "application/json"})
    upload_url = {k.lower(): v for k, v in headers.items()}.get("x-goog-upload-url")
    if status != 200 or not upload_url:
        raise GeminiError(f"upload start failed: {_err(status, body)}")
    status, _, body = _request(
        "POST", upload_url, key, body=video.read_bytes(), timeout=timeout,
        headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                 "X-Goog-Upload-Command": "upload, finalize"})
    if status != 200:
        raise GeminiError(f"upload failed: {_err(status, body)}")
    f = json.loads(body).get("file") or {}
    deadline = time.monotonic() + 180
    while f.get("state") == "PROCESSING":
        if time.monotonic() > deadline:
            raise GeminiError("video processing timed out")
        time.sleep(3)
        status, _, body = _request("GET", f"{BASE}/v1beta/{f['name']}", key)
        if status != 200:
            raise GeminiError(f"file status failed: {_err(status, body)}")
        f = json.loads(body)
    if f.get("state") not in (None, "ACTIVE"):
        raise GeminiError(f"video processing ended in state {f.get('state')}")
    return f


def delete(key: str, name: str) -> None:
    try:
        _request("DELETE", f"{BASE}/v1beta/{name}", key, timeout=30)
    except (urllib.error.URLError, OSError):
        pass  # files expire after 48 h anyway


# ---------------------------------------------------------------- model call
def interaction_text(resp: dict) -> str:
    """Model output text from an Interactions response (current 'steps', legacy 'outputs')."""
    for step in reversed(resp.get("steps") or []):
        if step.get("type") == "model_output":
            for part in step.get("content") or []:
                if part.get("type") == "text" and part.get("text"):
                    return part["text"]
    for out in resp.get("outputs") or []:
        if out.get("text"):
            return out["text"]
    return ""


def generate_content_text(resp: dict) -> str:
    for cand in resp.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            if part.get("text"):
                return part["text"]
    return ""


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def ask(cfg: dict, key: str, file: dict, prompt: str) -> tuple[dict, str]:
    g = cfg["gemini"]
    model = g.get("model", "gemini-3.8-flash")
    body = {
        "model": model,
        "input": [{"type": "text", "text": prompt},
                  {"type": "video", "uri": file["uri"], "mime_type": file.get("mimeType", "video/mp4")}],
        "response_format": {"type": "text", "mime_type": "application/json", "schema": SCHEMA},
        "generation_config": {"thinking_level": g.get("thinking_level", "low")},
    }
    status, _, raw = _request("POST", f"{BASE}/v1beta/interactions", key, body=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json", "Api-Revision": API_REVISION},
                              timeout=180)
    if status == 200:
        resp = json.loads(raw)
        if resp.get("status") not in (None, "completed"):
            raise GeminiError(f"interaction {resp.get('status')}")
        return _parse_json(interaction_text(resp)), model
    if status not in (400, 404):
        raise GeminiError(_err(status, raw))
    # fallback: the older generateContent endpoint
    log.info("gemini interactions call failed (%s); trying generateContent", _err(status, raw))
    body = {
        "contents": [{"parts": [{"file_data": {"mime_type": file.get("mimeType", "video/mp4"),
                                               "file_uri": file["uri"]}},
                                {"text": prompt + "\n\nAnswer with a single JSON object with exactly these keys: "
                                 + ", ".join(SCHEMA["required"]) + "."}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    status, _, raw = _request("POST", f"{BASE}/v1beta/models/{model}:generateContent", key,
                              body=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                              timeout=180)
    if status != 200:
        raise GeminiError(_err(status, raw))
    return _parse_json(generate_content_text(json.loads(raw))), model


def review(cfg: dict, video: Path, *, streamer: str, hook: str, transcript: str, chat_terms: list[str]) -> dict:
    """Ask Gemini about one rendered clip. Returns a normalised review dict; raises GeminiError."""
    prompt = PROMPT.format(streamer=streamer, hook=hook, chat=", ".join(chat_terms) or "n/a",
                           transcript=transcript[:1500] or "(no speech)")
    if cfg["gemini"].get("backend", "vertex") == "vertex":
        proxy = make_proxy(video)
        try:
            data, model = ask_vertex(cfg, proxy, prompt)
        finally:
            proxy.unlink(missing_ok=True)
    else:
        key = _key(cfg)
        if not key:
            raise GeminiError("GEMINI_API_KEY not set")
        f = upload(key, video)
        try:
            data, model = ask(cfg, key, f, prompt)
        finally:
            delete(key, f.get("name", ""))
    risk = str(data.get("spoiler_risk", "")).lower()
    return {
        "quality": max(1, min(10, int(data.get("visual_quality") or 1))),
        "what_happens": str(data.get("what_happens", ""))[:400],
        "spoiler": risk if risk in RISK_RANK else "low",
        "cutscene": bool(data.get("is_cutscene")),
        "unsafe": bool(data.get("unsafe")),
        "hook_matches": data.get("hook_matches") is not False,
        "better_hook": str(data.get("better_hook", ""))[:60],
        "reason": str(data.get("reason", ""))[:300],
        "model": model,
    }


# ---------------------------------------------------------------- combining
def combine(claude_quality: int, claude_spoiler: str, claude_unsafe: bool, gem: dict | None,
            weight: float = 0.6, disagree_points: int = 3) -> dict:
    """Final verdict from both models. gem=None means Gemini wasn't available."""
    if not gem:
        return {"quality": claude_quality, "spoiler": claude_spoiler, "unsafe": claude_unsafe, "disagree": False}
    if claude_spoiler in RISK_RANK:
        quality = round(weight * gem["quality"] + (1 - weight) * claude_quality)
        spoiler = max((claude_spoiler, gem["spoiler"]), key=lambda r: RISK_RANK[r])
        disagree = abs(gem["quality"] - claude_quality) >= disagree_points
    else:  # Claude wasn't available (template packaging): Gemini decides alone
        quality, spoiler, disagree = gem["quality"], gem["spoiler"], False
    if gem["cutscene"]:
        spoiler = "high"  # Rockstar takes down isolated cinematics
    return {"quality": quality, "spoiler": spoiler, "unsafe": claude_unsafe or gem["unsafe"],
            "disagree": disagree}
