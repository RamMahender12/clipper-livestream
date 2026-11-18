"""Instagram Reels uploader: Instagram Graph API content publishing (official only).

Docs checked 2026-11-18:
  https://developers.facebook.com/docs/instagram-platform/content-publishing/
  https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
  https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit
  https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights
  https://developers.facebook.com/docs/graph-api/changelog  (latest: v27.0, 2026-11-04;
      v23.0 still works until 2027-10-08)

Flow for a LOCAL file (resumable upload, documented as "only for apps that have
implemented Facebook Login for Business", so the host is graph.facebook.com and
the token is a Facebook user/system-user token, not an Instagram Login "IGAA..." one):
  1. GET  /<IG_ID>/content_publishing_limit?fields=config,quota_usage
  2. POST /<IG_ID>/media  media_type=REELS upload_type=resumable caption=... -> {id, uri}
  3. POST https://rupload.facebook.com/ig-api-upload/<ver>/<container>
         headers: Authorization: OAuth <token>, offset: 0, file_size: <bytes>; body = bytes
  4. GET  /<container>?fields=status_code   until FINISHED (doc: check <= once/min, <= 5 min)
  5. POST /<IG_ID>/media_publish creation_id=<container>
Limit: 100 API-published posts per rolling 24 h per account (enforced on media_publish).
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

from .base import Publisher, PostMeta, PostResult, PostStats, caption_with_tags

log = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
RUPLOAD_HOST = "https://rupload.facebook.com"
DEFAULT_VERSION = "v27.0"
POLL_SECONDS = 15
PROCESSING_TIMEOUT = 300

SETUP_STEPS = """\
1. Switch the Instagram account to a Professional account (Creator or Business) and link it
   to a Facebook Page you manage.
2. developers.facebook.com -> My Apps -> Create app (Business type) -> add the Instagram
   product and choose "API setup with Facebook login" (Facebook Login for Business). Local-file
   resumable uploads are not available with "Instagram login" (IGAA... tokens).
3. Permissions: instagram_basic, instagram_content_publish, instagram_manage_insights,
   pages_show_list, pages_read_engagement (+ business_management if the Page sits in a
   business portfolio). Posting to your OWN account works with Standard access while you have
   a role on the app; App Review + Business Verification are only needed to post for others.
4. Get a long-lived token: business.facebook.com -> Settings -> System users -> add one,
   assign it the app + Page + Instagram account, Generate token with the scopes above
   (does not expire). Alternative: Graph API Explorer user token -> exchange for a 60-day token.
5. Find the IG user id: GET https://graph.facebook.com/{version}/me/accounts?fields=instagram_business_account
   and put instagram_business_account.id into config.toml as instagram.ig_user_id.
6. export {token_env}=<token>   (keep it out of config.toml)"""


class _ApiError(Exception):
    """Short, token-free message for PostResult.error."""

    def __init__(self, msg: str, transient: bool = False):
        super().__init__(msg[:300])
        self.transient = transient


# ---------------------------------------------------------------- pure helpers


def instagram_container_params(meta: PostMeta, igcfg: dict) -> dict:
    params = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption_with_tags(meta, 2200),  # 2,200 chars, max 30 hashtags
        "share_to_feed": "true" if igcfg.get("share_to_feed", True) else "false",
    }
    if meta.is_paid_promotion and igcfg.get("paid_partnership_label", True):
        # "Paid partnership" label; documented as usable on its own (no sponsor ids)
        # and only with the Facebook Login flavour of the API, which is what we use.
        params["is_paid_partnership"] = "true"
    return params


def publishing_quota(resp: dict) -> tuple[int, int]:
    """(used, total) from a content_publishing_limit response."""
    data = resp.get("data")
    entry = data[0] if isinstance(data, list) and data else resp
    used = int(entry.get("quota_usage", 0) or 0)
    total = int((entry.get("config") or {}).get("quota_total", 100) or 100)
    return used, total


def parse_insights(resp: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in resp.get("data", []):
        value = (item.get("total_value") or {}).get("value")
        if value is None and item.get("values"):
            value = item["values"][0].get("value")
        if isinstance(value, (int, float)):
            out[item.get("name", "")] = int(value)
    return out


# ---------------------------------------------------------------- HTTP


def _graph_error(status: int, body: bytes) -> _ApiError:
    try:
        err = json.loads(body or b"{}").get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    msg = err.get("error_user_msg") or err.get("message") or "no details"
    code = err.get("code", "")
    sub = f"/{err['error_subcode']}" if err.get("error_subcode") else ""
    # 9007 = "media is not ready for publishing yet": worth another try after a pause.
    transient = bool(err.get("is_transient")) or status >= 500 or code in (1, 2, 4, 17, 32, 9007)
    return _ApiError(f"Instagram HTTP {status} (code {code}{sub}): {msg}", transient)


def _call(method: str, url: str, *, data: bytes | None = None, headers: dict | None = None,
          timeout: float = 60) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            raise _graph_error(exc.code, exc.read()) from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise _ApiError(f"Instagram connection error: {reason}", transient=True) from None
    try:
        return json.loads(body or b"{}")
    except ValueError:
        raise _ApiError("Instagram returned a non-JSON response") from None


def _with_retry(fn, attempts: int = 3):
    for i in range(attempts):
        try:
            return fn()
        except _ApiError as exc:
            if not exc.transient or i == attempts - 1:
                raise
            time.sleep(5 * (i + 1))


class InstagramPublisher(Publisher):
    name = "instagram"

    # -- config

    @property
    def _ig(self) -> dict:
        return self.cfg.get("instagram", {})

    def _version(self) -> str:
        return self._ig.get("graph_version") or DEFAULT_VERSION

    def _token(self) -> str:
        return os.environ.get(self._ig.get("access_token_env", "IG_ACCESS_TOKEN"), "").strip()

    def _get(self, path: str, **params) -> dict:
        params["access_token"] = self._token()
        url = f"{GRAPH_HOST}/{self._version()}/{path}?{urllib.parse.urlencode(params)}"
        return _with_retry(lambda: _call("GET", url))

    def _post(self, path: str, retry: bool = True, **params) -> dict:
        params["access_token"] = self._token()
        data = urllib.parse.urlencode(params).encode()
        url = f"{GRAPH_HOST}/{self._version()}/{path}"
        fn = lambda: _call("POST", url, data=data, headers={  # noqa: E731
            "Content-Type": "application/x-www-form-urlencoded"})
        return _with_retry(fn) if retry else fn()

    # -- interface

    def ready(self) -> tuple[bool, str]:
        env = self._ig.get("access_token_env", "IG_ACCESS_TOKEN")
        steps = SETUP_STEPS.format(token_env=env, version=self._version())
        if not str(self._ig.get("ig_user_id", "")).strip():
            return False, f"Instagram: instagram.ig_user_id is not set.\n{steps}"
        token = self._token()
        if not token:
            return False, f"Instagram: env var {env} is empty.\n{steps}"
        if token.startswith("IG"):
            return False, ("Instagram: this looks like an Instagram-Login token (IGAA...). Local "
                           "file uploads need a Facebook Login for Business token (EAA...).\n" + steps)
        return True, "instagram: ready"

    def publish(self, video: Path, meta: PostMeta) -> PostResult:
        try:
            ok, msg = self.ready()
            if not ok:
                return self._fail(msg.splitlines()[0])
            return self._publish(Path(video), meta)
        except Exception as exc:  # never crash the scheduler
            msg = str(exc) if isinstance(exc, _ApiError) else f"{type(exc).__name__}: {exc}"
            return self._fail(msg)

    def fetch_stats(self, remote_id: str) -> PostStats | None:
        try:
            fields = self._get(remote_id, fields="like_count,comments_count")
            # "views" replaced plays/impressions for Reels; a failure here (e.g. missing
            # instagram_manage_insights) returns None so stored view counts are not zeroed.
            insights = parse_insights(self._get(f"{remote_id}/insights", metric="views,shares"))
            if "views" not in insights:
                return None
            return PostStats(views=insights["views"], likes=int(fields.get("like_count", 0) or 0),
                             comments=int(fields.get("comments_count", 0) or 0),
                             shares=insights.get("shares", 0))
        except Exception as exc:
            log.warning("instagram stats %s failed: %s", remote_id, exc)
            return None

    # -- internals

    def _fail(self, msg: str) -> PostResult:
        return PostResult(ok=False, platform=self.name, status="error", error=msg[:300])

    def _publish(self, video: Path, meta: PostMeta) -> PostResult:
        if not video.is_file():
            return self._fail(f"video not found: {video}")
        size = video.stat().st_size
        ig_id = str(self._ig["ig_user_id"]).strip()

        used, total = publishing_quota(
            self._get(f"{ig_id}/content_publishing_limit", fields="config,quota_usage"))
        if used >= total:
            return self._fail(f"Instagram publishing limit reached ({used}/{total} in the last 24h)")

        container = self._post(f"{ig_id}/media", retry=False,
                               **instagram_container_params(meta, self._ig))
        container_id = container.get("id")
        upload_uri = container.get("uri") or (
            f"{RUPLOAD_HOST}/ig-api-upload/{self._version()}/{container_id}")
        if not container_id:
            return self._fail("Instagram did not return a container id")

        self._upload(upload_uri, video, size)
        self._wait_finished(container_id)

        published = self._post(f"{ig_id}/media_publish", creation_id=container_id)
        media_id = published.get("id")
        if not media_id:
            return self._fail("media_publish returned no media id")
        url = ""
        try:
            url = self._get(media_id, fields="permalink").get("permalink", "")
        except _ApiError as exc:
            log.warning("instagram permalink lookup failed: %s", exc)
        return PostResult(ok=True, platform=self.name, remote_id=media_id, url=url,
                          status="published")

    def _upload(self, uri: str, video: Path, size: int) -> None:
        def attempt() -> dict:
            with open(video, "rb") as fh:
                return _call("POST", uri, data=fh, timeout=600, headers={
                    "Authorization": f"OAuth {self._token()}",
                    "offset": "0",
                    "file_size": str(size),
                    "Content-Length": str(size),
                    "Content-Type": "application/octet-stream",
                })
        resp = _with_retry(attempt)
        if resp.get("success") is False:
            raise _ApiError(f"Instagram upload rejected: {resp.get('message', '')}")

    def _wait_finished(self, container_id: str) -> None:
        timeout = float(self._ig.get("processing_timeout", PROCESSING_TIMEOUT))
        deadline = time.monotonic() + timeout
        status = ""
        while True:
            info = self._get(container_id, fields="status_code,status")
            status = info.get("status_code", "")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise _ApiError(f"Instagram processing {status}: {info.get('status', '')}")
            if time.monotonic() + POLL_SECONDS > deadline:
                raise _ApiError(f"Instagram processing timed out after {int(timeout)}s "
                                f"(last status {status or 'unknown'})")
            time.sleep(POLL_SECONDS)
