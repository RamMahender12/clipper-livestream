"""TikTok uploader: TikTok Content Posting API (official endpoints only).

Docs checked 2026-11-18 (developers.tiktok.com refused TLS from this machine, so the
pages below were checked through the official-docs search index, not opened directly;
re-read them if TikTok changes something):
  https://developers.tiktok.com/doc/content-posting-api-get-started-upload-content
  https://developers.tiktok.com/doc/content-posting-api-reference-upload-video
  https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
  https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info
  https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status
  https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
  https://developers.tiktok.com/doc/login-kit-desktop
  https://developers.tiktok.com/doc/oauth-user-access-token-management

Modes (cfg["tiktok"]["mode"]):
  inbox  - POST /v2/post/publish/inbox/video/init/ (scope video.upload). The video lands
           in the creator's TikTok inbox as a draft; a human adds the caption, ticks
           "Content disclosure" for paid clips, and posts it in the app. The inbox
           endpoint takes no caption / disclosure fields. At most 5 pending inbox
           drafts per user per 24 h (error spam_risk_too_many_pending_share).
  direct - POST /v2/post/publish/video/init/ (scope video.publish). Until TikTok audits the
           app, every post is forced to SELF_ONLY (private) and at most 5 users may post
           per 24 h; branded content cannot be private, so paid clips need an audited app.

Login Kit for Desktop: redirect URIs must use localhost or 127.0.0.1 with an explicit port
(http allowed), PKCE is required, and the code_challenge is the HEX SHA-256 of the verifier
(not base64url as in RFC 7636). If the loopback listener cannot be used, call
authorize(cfg, manual=True) and paste the address the browser was redirected to.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import HTTPServer
from pathlib import Path

from .. import config as config_mod
from .base import Publisher, PostMeta, PostResult, PostStats, caption_with_tags
from .youtube import _CallbackHandler, _read_json, _wait_for_redirect, _write_secret_json

log = logging.getLogger(__name__)

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
DEFAULT_REDIRECT = "http://127.0.0.1:8766/callback/"

# Media transfer rules: every chunk 5 MB..64 MB except the last, which absorbs the
# remainder (up to 128 MB); total_chunk_count = floor(video_size / chunk_size);
# 1..1000 chunks; files under 5 MB go up whole; files over 64 MB must be chunked.
# The docs do not say whether "MB" is 10^6 or 2^20, so the bounds below satisfy both.
TT_MIN_CHUNK = 5 * 1024 * 1024
TT_MAX_CHUNK = 64 * 1000 * 1000
TT_MAX_FINAL_CHUNK = 128 * 1000 * 1000
TT_MAX_CHUNKS = 1000
TT_DEFAULT_CHUNK = 10_000_000

POLL_SECONDS = 10  # status/fetch allows 30 requests/min per token
MIMES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


class _ApiError(Exception):
    """Short, token-free message for PostResult.error."""


# ---------------------------------------------------------------- pure helpers


def tiktok_chunk_plan(video_size: int, preferred: int = TT_DEFAULT_CHUNK) -> tuple[int, int]:
    """Return (chunk_size, total_chunk_count) that satisfies TikTok's FILE_UPLOAD rules."""
    if video_size <= 0:
        raise ValueError("video is empty")
    chunk = max(TT_MIN_CHUNK, min(int(preferred), TT_MAX_CHUNK))
    if video_size <= chunk:
        return video_size, 1  # whole file in one request (mandatory below 5 MB)
    count = video_size // chunk
    if count > TT_MAX_CHUNKS:
        chunk = -(-video_size // TT_MAX_CHUNKS)  # ceil, so floor(size/chunk) <= 1000
        if chunk > TT_MAX_CHUNK:
            raise ValueError("video too large for TikTok FILE_UPLOAD")
        count = video_size // chunk
    if count == 1:
        # chunk < size < 2*chunk: send whole if allowed, else split in two halves.
        if video_size <= TT_MAX_CHUNK:
            return video_size, 1
        chunk = video_size // 2
        count = video_size // chunk
    return chunk, count


def tiktok_chunk_ranges(video_size: int, chunk_size: int, count: int) -> list[tuple[int, int]]:
    """Inclusive (first_byte, last_byte) per chunk; the last chunk takes the remainder."""
    ranges = []
    for i in range(count):
        start = i * chunk_size
        end = video_size - 1 if i == count - 1 else start + chunk_size - 1
        ranges.append((start, end))
    return ranges


def tiktok_code_challenge(verifier: str) -> str:
    """TikTok desktop PKCE: lowercase hex SHA-256 of the verifier."""
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()


def trim_utf16(text: str, limit: int) -> str:
    """TikTok counts caption length in UTF-16 code units."""
    while len(text.encode("utf-16-le")) // 2 > limit:
        text = text[:-1]
    return text


def tiktok_post_info(meta: PostMeta, tcfg: dict, creator: dict, privacy: str) -> dict:
    return {
        "title": trim_utf16(caption_with_tags(meta, 2200), 2200),
        "privacy_level": privacy,
        "disable_duet": bool(creator.get("duet_disabled", False)),
        "disable_comment": bool(creator.get("comment_disabled", False)),
        "disable_stitch": bool(creator.get("stitch_disabled", False)),
        "video_cover_timestamp_ms": int(tcfg.get("cover_timestamp_ms", 1000)),
        # brand_content_toggle = paid partnership promoting a third party (a paying
        # campaign); brand_organic_toggle = promoting your own business.
        "brand_content_toggle": bool(meta.is_paid_promotion),
        "brand_organic_toggle": False,
        "is_aigc": False,
    }


def tiktok_source_info(video_size: int) -> dict:
    chunk, count = tiktok_chunk_plan(video_size)
    return {"source": "FILE_UPLOAD", "video_size": video_size,
            "chunk_size": chunk, "total_chunk_count": count}


# ---------------------------------------------------------------- config / tokens


def _tcfg(cfg: dict) -> dict:
    return cfg.get("tiktok", {})


def _client(cfg: dict) -> tuple[str, str]:
    t = _tcfg(cfg)
    return (os.environ.get(t.get("client_key_env", "TIKTOK_CLIENT_KEY"), "").strip(),
            os.environ.get(t.get("client_secret_env", "TIKTOK_CLIENT_SECRET"), "").strip())


def _token_path(cfg: dict) -> Path:
    return config_mod.resolve(cfg, _tcfg(cfg).get("token_file", "secrets/tiktok_token.json"))


def _scopes(cfg: dict) -> list[str]:
    t = _tcfg(cfg)
    if t.get("scopes"):
        return list(t["scopes"])
    scopes = ["user.info.basic", "video.upload", "video.list"]  # video.list = view counts
    if t.get("mode", "inbox") == "direct":
        scopes.append("video.publish")
    return scopes


def _redirect_uri(cfg: dict) -> str:
    return _tcfg(cfg).get("redirect_uri", DEFAULT_REDIRECT)


def _setup_steps(cfg: dict) -> str:
    t = _tcfg(cfg)
    return f"""\
1. developers.tiktok.com -> Manage apps -> Connect an app. Fill in the app details (icon,
   description, terms/privacy URLs), add products "Login Kit", "Content Posting API" and
   "Display API" (for view counts).
2. Login Kit -> platform Desktop -> register the redirect URI exactly: {_redirect_uri(cfg)}
3. Scopes: {', '.join(_scopes(cfg))}. For direct posting also enable "Direct Post" in the
   Content Posting API settings. Submit the app for review; until it is approved only the
   sandbox / target users you add can authorize it.
4. Direct Post to public needs TikTok's separate Content Posting API audit; unaudited apps
   are limited to SELF_ONLY (private) posts. Inbox (draft) mode needs no audit.
5. export {t.get('client_key_env', 'TIKTOK_CLIENT_KEY')}=... and {t.get('client_secret_env', 'TIKTOK_CLIENT_SECRET')}=...
6. Run once: .venv/bin/python -c "from clipper import config; from clipper.publishers import tiktok; tiktok.authorize(config.load())"
   (add manual=True to paste the redirect address by hand). Token goes to {_token_path(cfg)}"""


def _oauth_post(fields: dict) -> dict:
    req = urllib.request.Request(
        TOKEN_URL, data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                raise _ApiError(f"TikTok OAuth HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise _ApiError(f"TikTok OAuth connection error: {getattr(exc, 'reason', exc)}") from None
    if payload.get("error") or not payload.get("access_token"):
        err = payload.get("error") or "no_access_token"
        if isinstance(err, dict):
            err = err.get("code", "error")
        raise _ApiError(f"TikTok OAuth {err}: {payload.get('error_description', '')}"[:300])
    return payload


def _save_token(cfg: dict, tok: dict) -> dict:
    now = time.time()
    data = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "open_id": tok.get("open_id", ""),
        "scope": tok.get("scope", ""),
        "expires_at": now + int(tok.get("expires_in", 86400)),
        "refresh_expires_at": now + int(tok.get("refresh_expires_in", 31536000)),
    }
    _write_secret_json(_token_path(cfg), data)
    return data


def _access_token(cfg: dict) -> str:
    tok = _read_json(_token_path(cfg))
    if not tok.get("refresh_token"):
        raise _ApiError("TikTok not authorized: run tiktok.authorize(cfg)")
    if tok.get("access_token") and tok.get("expires_at", 0) - 300 > time.time():
        return tok["access_token"]
    key, secret = _client(cfg)
    if not key or not secret:
        raise _ApiError("TikTok client key/secret env vars are not set")
    try:
        fresh = _oauth_post({"client_key": key, "client_secret": secret,
                             "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    except _ApiError as exc:
        if "invalid_grant" in str(exc):
            raise _ApiError("TikTok refresh token expired/revoked: re-run tiktok.authorize(cfg)") from None
        raise
    # Refresh tokens rotate: the response's refresh_token replaces the old one.
    return _save_token(cfg, fresh)["access_token"]


def authorize(cfg: dict, manual: bool = False, timeout: float = 300.0) -> None:
    """One-time interactive consent (PKCE, loopback redirect, manual paste fallback)."""
    key, secret = _client(cfg)
    if not key or not secret:
        raise RuntimeError("Set the TikTok client key/secret env vars first.\n" + _setup_steps(cfg))
    redirect_uri = _redirect_uri(cfg)
    verifier = secrets.token_urlsafe(64)  # 86 chars of [A-Za-z0-9_-]
    state = secrets.token_urlsafe(24)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": key,
        "scope": ",".join(_scopes(cfg)),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": tiktok_code_challenge(verifier),
        "code_challenge_method": "S256",
    })
    params = None
    server = None
    if not manual:
        parsed = urllib.parse.urlparse(redirect_uri)
        try:
            server = HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 80), _CallbackHandler)
        except OSError as exc:
            print(f"Could not listen on {redirect_uri} ({exc}); falling back to manual paste.")
            manual = True
    print("Opening your browser for TikTok consent. If it does not open, visit:\n" + url)
    webbrowser.open(url)
    if server is not None:
        try:
            params = _wait_for_redirect(server, timeout)
        finally:
            server.server_close()
        if params is None:
            raise RuntimeError("Timed out waiting for the TikTok redirect; retry with manual=True.")
    else:
        pasted = input("After approving, copy the FULL address your browser was sent to "
                       "(it may show an error page) and paste it here:\n> ").strip()
        query = urllib.parse.urlparse(pasted).query or pasted
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
    if params.get("error"):
        raise RuntimeError(f"TikTok consent failed: {params.get('error')} "
                           f"{params.get('error_description', '')}")
    if params.get("state") != state:
        raise RuntimeError("OAuth state mismatch; aborting.")
    if not params.get("code"):
        raise RuntimeError("No authorization code in the redirect.")
    tok = _oauth_post({"client_key": key, "client_secret": secret, "code": params["code"],
                       "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                       "code_verifier": verifier})
    _save_token(cfg, tok)
    print(f"TikTok authorized (scopes: {tok.get('scope', '')}). Token saved to {_token_path(cfg)}")


# ---------------------------------------------------------------- HTTP


def _api(path: str, token: str, body: dict | None = None, query: dict | None = None) -> dict:
    """POST a JSON request to the v2 API and return payload["data"]."""
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    status = 200
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise _ApiError(f"TikTok connection error: {getattr(exc, 'reason', exc)}") from None
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise _ApiError(f"TikTok HTTP {status}: non-JSON response") from None
    err = payload.get("error") or {}
    code = err.get("code", "ok") if isinstance(err, dict) else str(err)
    if status >= 400 or code != "ok":
        msg = err.get("message", "") if isinstance(err, dict) else ""
        raise _ApiError(f"TikTok {code} (HTTP {status}): {msg}"[:300])
    return payload.get("data") or {}


def _put_chunk(upload_url: str, data: bytes, start: int, end: int, total: int, mime: str) -> None:
    headers = {"Content-Type": mime, "Content-Length": str(len(data)),
               "Content-Range": f"bytes {start}-{end}/{total}"}
    detail = ""
    for attempt in range(1, 6):
        req = urllib.request.Request(upload_url, data=data, method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                if resp.status in (200, 201, 206):
                    return
                detail = f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            with exc:
                if exc.code < 500 and exc.code != 429:
                    hint = " (upload_url expired)" if exc.code == 403 else ""
                    raise _ApiError(f"TikTok chunk upload HTTP {exc.code}{hint}") from None
                detail = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            detail = f"connection error: {getattr(exc, 'reason', exc)}"
        time.sleep(min(30, 2 ** attempt))
    raise _ApiError(f"TikTok chunk {start}-{end} failed after retries ({detail})")


# ---------------------------------------------------------------- publisher


class TikTokPublisher(Publisher):
    name = "tiktok"

    @property
    def _mode(self) -> str:
        return _tcfg(self.cfg).get("mode", "inbox")

    def ready(self) -> tuple[bool, str]:
        key, secret = _client(self.cfg)
        if not key or not secret:
            return False, "TikTok: client key/secret env vars are empty.\n" + _setup_steps(self.cfg)
        if self._mode not in ("inbox", "direct"):
            return False, f"TikTok: tiktok.mode must be 'inbox' or 'direct', not {self._mode!r}"
        tok = _read_json(_token_path(self.cfg))
        if not tok.get("refresh_token"):
            return False, "TikTok: not authorized yet.\n" + _setup_steps(self.cfg)
        if tok.get("refresh_expires_at", float("inf")) < time.time():
            return False, "TikTok: refresh token expired; re-run tiktok.authorize(cfg).\n" + _setup_steps(self.cfg)
        needed = "video.publish" if self._mode == "direct" else "video.upload"
        granted = {s.strip() for s in str(tok.get("scope", "")).split(",") if s.strip()}
        if granted and needed not in granted:
            return False, f"TikTok: token lacks scope {needed}; re-run tiktok.authorize(cfg)."
        if self._mode == "direct":
            return True, ("tiktok: ready (direct). WARNING: until TikTok audits this app, Direct Post "
                          "only accepts SELF_ONLY (private) posts, max 5 posting users per 24h, and "
                          "paid/branded clips are rejected. Use mode='inbox' to post publicly by hand.")
        return True, ("tiktok: ready (inbox). Each clip lands as a draft in the TikTok inbox; open the "
                      "app to add the caption, tick Content disclosure for paid clips, and post. "
                      "Max 5 pending drafts per 24h.")

    def publish(self, video: Path, meta: PostMeta) -> PostResult:
        try:
            return self._publish(Path(video), meta)
        except Exception as exc:  # never crash the scheduler
            msg = str(exc) if isinstance(exc, _ApiError) else f"{type(exc).__name__}: {exc}"
            return PostResult(ok=False, platform=self.name, status="error", error=msg[:300])

    def fetch_stats(self, remote_id: str) -> PostStats | None:
        try:
            token = _access_token(self.cfg)
            video_id = remote_id if remote_id.isdigit() else self._post_id(token, remote_id)
            if not video_id:
                return None  # draft not posted yet, or posted privately
            data = _api("/video/query/", token, {"filters": {"video_ids": [video_id]}},
                        query={"fields": "id,view_count,like_count,comment_count,share_count"})
            videos = data.get("videos") or []
            if not videos:
                return None
            v = videos[0]
            return PostStats(views=int(v.get("view_count", 0)), likes=int(v.get("like_count", 0)),
                             comments=int(v.get("comment_count", 0)), shares=int(v.get("share_count", 0)))
        except Exception as exc:
            log.warning("tiktok stats %s failed: %s", remote_id, exc)
            return None

    # -- internals

    def _post_id(self, token: str, publish_id: str) -> str:
        data = _api("/post/publish/status/fetch/", token, {"publish_id": publish_id})
        ids = data.get("publicaly_available_post_id") or []  # sic: TikTok's spelling
        return str(ids[0]) if ids else ""

    def _publish(self, video: Path, meta: PostMeta) -> PostResult:
        ok, msg = self.ready()
        if not ok:
            raise _ApiError(msg.splitlines()[0])
        if not video.is_file():
            raise _ApiError(f"video not found: {video}")
        size = video.stat().st_size
        source = tiktok_source_info(size)
        mime = MIMES.get(video.suffix.lower(), "video/mp4")
        token = _access_token(self.cfg)
        tcfg = _tcfg(self.cfg)

        username = ""
        if self._mode == "direct":
            creator = _api("/post/publish/creator_info/query/", token)
            username = creator.get("creator_username", "")
            options = creator.get("privacy_level_options") or []
            privacy = tcfg.get("privacy_level", "SELF_ONLY")
            if options and privacy not in options:
                raise _ApiError(f"TikTok privacy_level {privacy} not allowed for this creator "
                                f"(options: {', '.join(options)})")
            if meta.is_paid_promotion and privacy == "SELF_ONLY":
                raise _ApiError("TikTok rejects branded content with SELF_ONLY privacy; use "
                                "mode='inbox' (tick Content disclosure in the app) or an audited "
                                "app with a public privacy_level")
            body = {"post_info": tiktok_post_info(meta, tcfg, creator, privacy), "source_info": source}
            init = _api("/post/publish/video/init/", token, body)
        else:
            init = _api("/post/publish/inbox/video/init/", token, {"source_info": source})

        publish_id, upload_url = init.get("publish_id", ""), init.get("upload_url", "")
        if not publish_id or not upload_url:
            raise _ApiError("TikTok init returned no publish_id/upload_url")

        with open(video, "rb") as fh:
            for start, end in tiktok_chunk_ranges(size, source["chunk_size"], source["total_chunk_count"]):
                fh.seek(start)
                _put_chunk(upload_url, fh.read(end - start + 1), start, end, size, mime)

        final = self._wait(token, publish_id)
        if self._mode == "inbox":
            return PostResult(ok=True, platform=self.name, remote_id=publish_id, status="draft")
        ids = final.get("publicaly_available_post_id") or []
        post_id = str(ids[0]) if ids else ""
        url = f"https://www.tiktok.com/@{username}/video/{post_id}" if post_id and username else ""
        return PostResult(ok=True, platform=self.name, remote_id=post_id or publish_id, url=url,
                          status="published")

    def _wait(self, token: str, publish_id: str) -> dict:
        """Poll status/fetch until the upload is handed off; raise on FAILED."""
        done = {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}
        timeout = float(_tcfg(self.cfg).get("processing_timeout", 300))
        deadline = time.monotonic() + timeout
        data: dict = {}
        while time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            try:
                data = _api("/post/publish/status/fetch/", token, {"publish_id": publish_id})
            except _ApiError as exc:
                log.warning("tiktok status %s: %s", publish_id, exc)
                continue
            status = data.get("status", "")
            if status == "FAILED":
                raise _ApiError(f"TikTok processing failed: {data.get('fail_reason', 'unknown')}")
            if status in done:
                return data
        # Bytes are uploaded; TikTok is still processing. Report success rather than
        # re-posting a duplicate; fetch_stats resolves the post id later.
        log.warning("tiktok %s still processing after %ss", publish_id, int(timeout))
        return data
