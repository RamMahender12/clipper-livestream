"""YouTube Shorts uploader: YouTube Data API v3 (official endpoints only).

Docs checked 2026-11-18:
  https://developers.google.com/youtube/v3/docs/videos/insert
  https://developers.google.com/youtube/v3/docs/videos            (status.*, paidProductPlacementDetails)
  https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
  https://developers.google.com/youtube/v3/revision_history
      2026-11-02: uploads from unverified API projects are no longer forced to private.
      2026-11-16: paidProductPlacementDetails.hasPaidProductPlacement no longer defaults
                  to false, so we always send it explicitly.
  https://developers.google.com/youtube/v3/determine_quota_cost   (videos.insert has its own
      bucket: 1 unit/call, default 100 uploads/day per Cloud project)
  https://developers.google.com/identity/protocols/oauth2/native-app  (loopback redirect + PKCE;
      manual copy/paste "OOB" flow is no longer supported)

A vertical video up to 3 minutes is classified as a Short automatically; there is
no separate Shorts endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .. import config as config_mod
from .base import Publisher, PostMeta, PostResult, PostStats, caption_with_tags

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # videos.list statistics
]
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_URL = "https://www.googleapis.com/youtube/v3"
UPLOAD_PARTS = "snippet,status,paidProductPlacementDetails"

CHUNK_SIZE = 8 * 1024 * 1024  # must be a multiple of 256 KiB (except the last chunk)
RETRY_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 8
PRIVACY_VALUES = {"public", "unlisted", "private"}

AUTH_CMD = ('.venv/bin/python -c "from clipper import config; '
            'from clipper.publishers import youtube; youtube.authorize(config.load())"')
SETUP_STEPS = """\
1. console.cloud.google.com -> create/select a project -> APIs & Services -> Library ->
   enable "YouTube Data API v3".
2. Google Auth Platform (OAuth consent screen): user type External; add the scopes
   .../auth/youtube.upload and .../auth/youtube.readonly; add your Google account as a test user.
   Then click "Publish app" (status "In production"): refresh tokens of apps left in
   "Testing" expire after 7 days. Google verification is not needed for your own channel;
   you will click through an "unverified app" warning once.
3. Clients -> Create client -> Application type "Desktop app" -> Download JSON and save it as
   {secrets_file}
4. Run once and approve with the Google account that owns the channel:
   {auth_cmd}
   (token saved to {token_file})"""


class _ApiError(Exception):
    """Short, token-free error message suitable for PostResult.error."""


class _SessionExpired(Exception):
    pass


# ---------------------------------------------------------------- HTTP helpers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # The resumable protocol answers 308 "Resume Incomplete"; never treat it as a redirect.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(method: str, url: str, *, data: bytes | None = None,
             headers: dict | None = None, timeout: float = 60):
    """Return (status, headers, body). Raises only on connection-level failures."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.headers, exc.read()


def _error_text(status: int, body: bytes) -> str:
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        payload = {}
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        reason = ((err.get("errors") or [{}])[0] or {}).get("reason", "")
        msg = f"YouTube HTTP {status} {reason}: {err.get('message', '')}"
    elif isinstance(err, str):
        msg = f"Google OAuth HTTP {status} {err}: {payload.get('error_description', '')}"
    else:
        msg = f"YouTube HTTP {status}"
    return msg.strip().rstrip(":")[:300]


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    status, _, body = _request("POST", url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        raise _ApiError(_error_text(status, body))
    return json.loads(body)


# ---------------------------------------------------------------- pure helpers


def pkce_pair() -> tuple[str, str]:
    """RFC 7636 verifier + S256 challenge (base64url, no padding)."""
    verifier = secrets.token_urlsafe(64)  # 86 chars from the unreserved set
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def trim_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", "ignore")


def _no_angle(text: str) -> str:
    # The API rejects "<" and ">" in titles and descriptions.
    return text.replace("<", "").replace(">", "")


def youtube_video_body(meta: PostMeta, ycfg: dict) -> dict:
    title = " ".join(_no_angle(meta.title or "").split())[:100].strip() or "GTA 6 clip"
    description = trim_utf8(_no_angle(caption_with_tags(meta, 5000)), 5000)  # limit is 5000 bytes
    tags, used = [], 0
    for t in meta.hashtags:
        t = _no_angle(t.lstrip("#").strip())
        if t and used + len(t) + 1 <= 400:  # tags total must stay under 500 chars
            tags.append(t)
            used += len(t) + 1
    snippet = {"title": title, "description": description,
               "categoryId": str(ycfg.get("category_id", "20"))}
    if tags:
        snippet["tags"] = tags
    return {
        "snippet": snippet,
        "status": {
            "privacyStatus": ycfg.get("privacy", "public"),
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": bool(ycfg.get("contains_synthetic_media", False)),
        },
        # The API's equivalent of Studio's "paid promotion" checkbox. Sent every time
        # because, since 2026-11-16, omitting it no longer means "false".
        "paidProductPlacementDetails": {"hasPaidProductPlacement": bool(meta.is_paid_promotion)},
    }


def youtube_init_request(meta: PostMeta, ycfg: dict, size: int,
                         mime: str = "video/mp4") -> tuple[str, dict, bytes]:
    """(url, headers, body) for the resumable-session POST, minus Authorization."""
    query = urllib.parse.urlencode({"uploadType": "resumable", "part": UPLOAD_PARTS})
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size),
        "X-Upload-Content-Type": mime,
    }
    body = json.dumps(youtube_video_body(meta, ycfg)).encode("utf-8")
    return f"{UPLOAD_URL}?{query}", headers, body


def content_range(start: int, length: int, total: int) -> str:
    return f"bytes {start}-{start + length - 1}/{total}"


def next_offset(range_header: str | None) -> int:
    """Byte to send next, from a 308 response's Range header ("bytes=0-999" -> 1000)."""
    if not range_header or "-" not in range_header:
        return 0
    return int(range_header.rsplit("-", 1)[1].strip()) + 1


# ---------------------------------------------------------------- tokens


def _paths(cfg: dict) -> tuple[Path, Path]:
    y = cfg.get("youtube", {})
    return (config_mod.resolve(cfg, y.get("client_secrets_file", "secrets/youtube_client_secret.json")),
            config_mod.resolve(cfg, y.get("token_file", "secrets/youtube_token.json")))


def _load_client(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    client = data.get("installed")
    if not client:
        raise ValueError("client secrets JSON has no 'installed' section; create a "
                         "'Desktop app' OAuth client, not 'Web application'")
    if not client.get("client_id"):
        raise ValueError("client secrets JSON has no client_id")
    return client


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_secret_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _store_token(path: Path, tok: dict, previous: dict | None = None) -> dict:
    merged = dict(previous or {})
    merged.update({k: v for k, v in tok.items() if k in (
        "access_token", "refresh_token", "scope", "token_type")})
    merged["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
    _write_secret_json(path, merged)
    return merged


def _access_token(cfg: dict, force: bool = False) -> str:
    secrets_path, token_path = _paths(cfg)
    tok = _read_json(token_path)
    if not tok.get("refresh_token"):
        raise _ApiError("YouTube not authorized: run youtube.authorize(cfg)")
    if not force and tok.get("access_token") and tok.get("expires_at", 0) - 60 > time.time():
        return tok["access_token"]
    client = _load_client(secrets_path)
    fields = {"client_id": client["client_id"], "grant_type": "refresh_token",
              "refresh_token": tok["refresh_token"]}
    if client.get("client_secret"):
        fields["client_secret"] = client["client_secret"]
    try:
        fresh = _post_form(TOKEN_URI, fields)
    except _ApiError as exc:
        if "invalid_grant" in str(exc):
            raise _ApiError("YouTube refresh token revoked/expired: re-run youtube.authorize(cfg)") from None
        raise
    return _store_token(token_path, fresh, tok)["access_token"]


# ---------------------------------------------------------------- consent flow


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        query = urllib.parse.urlparse(self.path).query
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
        if "code" in params or "error" in params:
            self.server.oauth_params = params  # type: ignore[attr-defined]
            status, body = 200, b"<h3>Authorization received. You can close this tab.</h3>"
        else:
            status, body = 404, b"not found"
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the auth code out of the console
        pass


def _wait_for_redirect(server: HTTPServer, timeout: float) -> dict | None:
    server.oauth_params = None  # type: ignore[attr-defined]
    server.timeout = 1.0
    deadline = time.monotonic() + timeout
    while server.oauth_params is None and time.monotonic() < deadline:  # type: ignore[attr-defined]
        server.handle_request()
    return server.oauth_params  # type: ignore[attr-defined]


def authorize(cfg: dict, timeout: float = 300.0) -> None:
    """One-time interactive consent: opens the browser, catches the loopback
    redirect on http://127.0.0.1:<random port>, stores the refresh token."""
    secrets_path, token_path = _paths(cfg)
    client = _load_client(secrets_path)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        url = AUTH_URI + "?" + urllib.parse.urlencode({
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",  # always return a refresh token, even on re-auth
        })
        print("Opening your browser for Google consent. If it does not open, visit:\n" + url)
        webbrowser.open(url)
        params = _wait_for_redirect(server, timeout)
    finally:
        server.server_close()
    if not params:
        raise RuntimeError("Timed out waiting for the Google consent redirect.")
    if params.get("error"):
        raise RuntimeError(f"Google consent failed: {params['error']}")
    if params.get("state") != state:
        raise RuntimeError("OAuth state mismatch; aborting.")
    fields = {"client_id": client["client_id"], "code": params["code"],
              "code_verifier": verifier, "grant_type": "authorization_code",
              "redirect_uri": redirect_uri}
    if client.get("client_secret"):
        fields["client_secret"] = client["client_secret"]
    tok = _post_form(TOKEN_URI, fields)
    if not tok.get("refresh_token"):
        raise RuntimeError("Google returned no refresh token. Remove the app at "
                           "https://myaccount.google.com/permissions and run authorize again.")
    _store_token(token_path, tok)
    print(f"YouTube authorized. Token saved to {token_path}")


# ---------------------------------------------------------------- publisher


class YouTubePublisher(Publisher):
    name = "youtube"

    def _setup(self) -> str:
        secrets_path, token_path = _paths(self.cfg)
        return SETUP_STEPS.format(secrets_file=secrets_path, token_file=token_path,
                                  auth_cmd=AUTH_CMD)

    def ready(self) -> tuple[bool, str]:
        secrets_path, token_path = _paths(self.cfg)
        try:
            _load_client(secrets_path)
        except FileNotFoundError:
            return False, f"YouTube: client secrets file missing ({secrets_path}).\n{self._setup()}"
        except (OSError, ValueError) as exc:
            return False, f"YouTube: bad client secrets file: {exc}\n{self._setup()}"
        if not _read_json(token_path).get("refresh_token"):
            return False, f"YouTube: not authorized yet (no {token_path.name}).\n{self._setup()}"
        privacy = self.cfg.get("youtube", {}).get("privacy", "public")
        if privacy not in PRIVACY_VALUES:
            return False, f"YouTube: youtube.privacy must be one of {sorted(PRIVACY_VALUES)}"
        return True, "youtube: ready"

    def publish(self, video: Path, meta: PostMeta) -> PostResult:
        try:
            video_id = self._publish(Path(video), meta)
            return PostResult(ok=True, platform=self.name, remote_id=video_id,
                              url=f"https://youtube.com/shorts/{video_id}", status="published")
        except Exception as exc:  # never crash the scheduler
            msg = str(exc) if isinstance(exc, _ApiError) else f"{type(exc).__name__}: {exc}"
            return PostResult(ok=False, platform=self.name, status="error", error=msg[:300])

    def fetch_stats(self, remote_id: str) -> PostStats | None:
        try:
            query = urllib.parse.urlencode({"part": "statistics", "id": remote_id})
            status, _, body = _request("GET", f"{API_URL}/videos?{query}", headers={
                "Authorization": f"Bearer {_access_token(self.cfg)}"})
            if status != 200:
                log.warning("youtube stats %s: %s", remote_id, _error_text(status, body))
                return None
            items = json.loads(body).get("items") or []
            if not items:
                return None
            s = items[0].get("statistics", {})
            return PostStats(views=int(s.get("viewCount", 0)), likes=int(s.get("likeCount", 0)),
                             comments=int(s.get("commentCount", 0)))
        except Exception as exc:
            log.warning("youtube stats %s failed: %s", remote_id, exc)
            return None

    def check_status(self, remote_id: str) -> tuple[str, str]:
        """Is the Short still up? Removed (missing), rejected (with YouTube's reason), blocked in some
        countries (usually a music claim), or age-restricted."""
        try:
            query = urllib.parse.urlencode({"part": "status,contentDetails", "id": remote_id})
            status, _, body = _request("GET", f"{API_URL}/videos?{query}", headers={
                "Authorization": f"Bearer {_access_token(self.cfg)}"})
        except Exception as exc:
            return "unknown", str(exc)[:150]
        if status != 200:
            return "unknown", _error_text(status, body)[:150]
        items = json.loads(body).get("items") or []
        if not items:
            return "removed", "the video no longer exists on YouTube (deleted or taken down)"
        st, cd = items[0].get("status", {}), items[0].get("contentDetails", {})
        if st.get("uploadStatus") in ("rejected", "deleted", "failed"):
            why = st.get("rejectionReason") or st.get("failureReason") or st.get("uploadStatus")
            return "rejected", f"YouTube {st.get('uploadStatus')} it: {why}"
        blocked = (cd.get("regionRestriction") or {}).get("blocked") or []
        if blocked:
            return "blocked", f"blocked in {len(blocked)} countries (usually a music or Content ID claim)"
        if (cd.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
            return "age", "age-restricted by YouTube"
        return "ok", ""

    # -- internals

    def _publish(self, video: Path, meta: PostMeta) -> str:
        if not video.is_file():
            raise _ApiError(f"video not found: {video}")
        size = video.stat().st_size
        if size <= 0:
            raise _ApiError(f"video is empty: {video}")
        mime = "video/mp4" if video.suffix.lower() == ".mp4" else "application/octet-stream"
        ycfg = self.cfg.get("youtube", {})
        for _ in range(2):  # a 404 mid-upload means the session expired: start over once
            session = self._start_session(meta, ycfg, size, mime)
            try:
                resource = self._upload(session, video, size, mime)
            except _SessionExpired:
                continue
            video_id = resource.get("id")
            if not video_id:
                raise _ApiError("upload finished but YouTube returned no video id")
            return video_id
        raise _ApiError("YouTube upload session expired twice")

    def _start_session(self, meta: PostMeta, ycfg: dict, size: int, mime: str) -> str:
        url, headers, body = youtube_init_request(meta, ycfg, size, mime)
        detail = ""
        for attempt in range(1, 5):
            hdrs = dict(headers, Authorization=f"Bearer {_access_token(self.cfg)}")
            try:
                status, resp_headers, resp_body = _request("POST", url, data=body, headers=hdrs)
            except (urllib.error.URLError, OSError) as exc:
                status, resp_headers, resp_body, detail = None, None, b"", f"connection error: {exc}"
            if status == 200 and resp_headers.get("Location"):
                return resp_headers["Location"]
            if status is not None and status not in RETRY_STATUS:
                raise _ApiError(_error_text(status, resp_body))
            detail = detail if status is None else _error_text(status, resp_body)
            time.sleep(min(30, 2 ** attempt) + random.random())
        raise _ApiError(f"could not start YouTube upload session ({detail})"[:300])

    def _put(self, session: str, data: bytes, crange: str, mime: str, force_refresh=False):
        headers = {"Authorization": f"Bearer {_access_token(self.cfg, force=force_refresh)}",
                   "Content-Type": mime, "Content-Range": crange}
        try:
            return _request("PUT", session, data=data, headers=headers, timeout=300)
        except (urllib.error.URLError, OSError) as exc:  # includes timeouts / resets
            return None, None, f"connection error: {exc}".encode()

    def _upload(self, session: str, video: Path, size: int, mime: str) -> dict:
        offset, probe, attempt, refreshed = 0, False, 0, False
        budget = size // CHUNK_SIZE + 4 * MAX_RETRIES + 8  # hard stop against odd server loops
        with open(video, "rb") as fh:
            for _ in range(budget):
                if probe or offset >= size:
                    # Ask the server how much it has: empty PUT with "bytes */TOTAL".
                    status, headers, body = self._put(session, b"", f"bytes */{size}", mime, refreshed)
                else:
                    fh.seek(offset)
                    chunk = fh.read(CHUNK_SIZE)
                    status, headers, body = self._put(
                        session, chunk, content_range(offset, len(chunk), size), mime, refreshed)
                refreshed = False
                if status in (200, 201):
                    return json.loads(body)
                if status == 308:
                    offset, probe, attempt = next_offset(headers.get("Range")), False, 0
                    continue
                if status == 404:
                    raise _SessionExpired()
                if status == 401 and attempt == 0:
                    refreshed, probe, attempt = True, True, 1
                    continue
                if status is None or status in RETRY_STATUS:
                    attempt += 1
                    if attempt > MAX_RETRIES:
                        why = body.decode() if status is None else _error_text(status, body)
                        raise _ApiError(f"YouTube upload failed after retries: {why}"[:300])
                    wait = min(64, 2 ** attempt) + random.random()
                    retry_after = headers.get("Retry-After") if headers else None
                    if retry_after and retry_after.isdigit():
                        wait = max(wait, int(retry_after))
                    time.sleep(wait)
                    probe = True  # never assume how many bytes arrived
                    continue
                raise _ApiError(_error_text(status, body))
        raise _ApiError("YouTube upload did not complete")
