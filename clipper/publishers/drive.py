"""Google Drive upload for exported clips (drive.file scope: the app only sees files it made).
One-time sign-in: python -m clipper.publishers.drive
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from pathlib import Path

from .. import config as config_mod
from . import youtube as yt

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
API = "https://www.googleapis.com"
_folders: dict[str, str] = {}  # ponytail: in-process cache; a deleted folder needs a restart


def _paths(cfg: dict) -> tuple[Path, Path]:
    d = cfg.get("drive", {})
    return (config_mod.resolve(cfg, d.get("client_secrets_file", "secrets/drive_client_secret.json")),
            config_mod.resolve(cfg, d.get("token_file", "secrets/drive_token.json")))


def enabled(cfg: dict) -> bool:
    return bool(yt._read_json(_paths(cfg)[1]).get("refresh_token")) and cfg.get("drive", {}).get("enabled", True)


def _call(cfg: dict, method: str, url: str, data: bytes | None = None, headers: dict | None = None):
    h = {"Authorization": "Bearer " + yt._access_token(cfg, paths=_paths(cfg)), **(headers or {})}
    status, hdrs, body = yt._request(method, url, data=data, headers=h, timeout=300)
    if status >= 300:
        raise RuntimeError(f"Drive HTTP {status}: {body[:200]!r}")
    return hdrs, (json.loads(body) if body else {})


def _folder(cfg: dict, name: str, parent: str | None) -> str:
    key = f"{parent}/{name}"
    if key in _folders:
        return _folders[key]
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    q += f" and '{parent}' in parents" if parent else ""
    _, found = _call(cfg, "GET", f"{API}/drive/v3/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}))
    if found.get("files"):
        fid = found["files"][0]["id"]
    else:
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            meta["parents"] = [parent]
        _, made = _call(cfg, "POST", f"{API}/drive/v3/files?fields=id", json.dumps(meta).encode(),
                        {"Content-Type": "application/json"})
        fid = made["id"]
    _folders[key] = fid
    return fid


def upload(cfg: dict, video: Path, day: str, name: str | None = None) -> str:
    """Upload to <folder>/<day>/ and return the file's Drive link."""
    root = _folder(cfg, cfg.get("drive", {}).get("folder", "GTA6 Clips"), None)
    parent = _folder(cfg, day, root)
    meta = json.dumps({"name": name or video.name, "parents": [parent]}).encode()
    hdrs, _ = _call(cfg, "POST", f"{API}/upload/drive/v3/files?uploadType=resumable&fields=webViewLink", meta,
                    {"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Type": "video/mp4"})
    _, done = _call(cfg, "PUT", hdrs["Location"], video.read_bytes(), {"Content-Type": "video/mp4"})
    return done["webViewLink"]


if __name__ == "__main__":
    c = config_mod.load()
    yt.authorize(c, scopes=SCOPES, paths=_paths(c))
