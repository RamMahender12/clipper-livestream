"""Harvest the clips Twitch viewers make: they've already picked the moment.

Every `interval_minutes`, for each streamer you're allowed to clip, ask
Twitch's official Helix API for the most-viewed clips of the last few hours,
download the best new ones (streamlink, source quality), and queue them
into the normal pipeline: captions, Claude packaging, Gemini review, then
auto-approve or your review bot. The viewer's clip title and view count go
along as context.

Read-only app token (client credentials). Secrets: TWITCH_CLIENT_ID,
TWITCH_CLIENT_SECRET. Docs: dev.twitch.tv/docs/api/reference#get-clips,
dev.twitch.tv/docs/authentication/getting-tokens-oauth (client credentials).
"""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod, guardrails, pipeline, tools
from .db import DB

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"

SCHEMA = """
CREATE TABLE IF NOT EXISTS twitch_clips_seen (
    id TEXT PRIMARY KEY,
    channel TEXT,
    clip_id INTEGER,
    views INTEGER,
    note TEXT DEFAULT '',
    seen_at REAL NOT NULL
);
"""


def configured() -> bool:
    return bool(os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"))


class Helix:
    def __init__(self):
        self.client_id = os.environ.get("TWITCH_CLIENT_ID", "")
        self.secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
        self._token, self._expires = "", 0.0
        self._ids: dict[str, str] = {}

    def token(self) -> str:
        if self._token and self._expires > time.time() + 300:
            return self._token
        body = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.secret,
                                       "grant_type": "client_credentials"}).encode()
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=30) as resp:
            tok = json.load(resp)
        self._token, self._expires = tok["access_token"], time.time() + int(tok.get("expires_in", 3600))
        return self._token

    def get(self, path: str, params: list[tuple[str, str]]) -> dict:
        url = f"{HELIX}/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Client-Id": self.client_id,
                                                   "Authorization": f"Bearer {self.token()}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def broadcaster_id(self, login: str) -> str | None:
        if login not in self._ids:
            data = self.get("users", [("login", login)]).get("data", [])
            if not data:
                return None
            self._ids[login] = data[0]["id"]
        return self._ids[login]

    def clips(self, broadcaster_id: str, since: datetime, first: int = 50) -> list[dict]:
        out = self.get("clips", [("broadcaster_id", broadcaster_id), ("first", str(first)),
                                 ("started_at", since.strftime("%Y-%m-%dT%H:%M:%SZ"))])
        return sorted(out.get("data", []), key=lambda c: c.get("view_count", 0), reverse=True)


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def download(clip: dict, out: Path) -> Path:
    """Fetch the viewer clip at source quality with streamlink."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tools.run([tools.streamlink(), "--loglevel", "error", "--force", "-o", str(out), clip["url"], "best"],
              timeout=180)
    if not out.exists() or out.stat().st_size < 50_000:
        raise RuntimeError("download produced no video")
    return out


def score_for(views: int) -> float:
    """Comparable with chat-spike scores: a well-viewed clip ranks like a strong spike."""
    return round(4 + 3 * math.log10(max(views, 1)), 2)


def rank_in(views: int, pool: list[int], min_pool: int = 5) -> tuple[int, float | None]:
    """(rank, share of the other clips it out-viewed). Share is None when the pool is too small."""
    rank = 1 + sum(1 for v in pool if v > views)
    if len(pool) < max(min_pool, 2):
        return rank, None
    return rank, round(sum(1 for v in pool if v < views) / (len(pool) - 1), 3)


def evidence(cfg: dict, clip: dict) -> str | None:
    """What Twitch viewers' reaction says about this clip: 'strong', 'weak', 'neutral', or None
    for clips we found ourselves."""
    views = int(clip.get("source_views") or 0)
    if not views:
        return None
    t = cfg["twitch_clips"]
    pct = clip.get("source_pct")
    if pct is None:
        return "neutral"
    if views >= int(t.get("strong_min_views", 50)) and pct >= 1 - float(t.get("strong_top", 0.2)):
        return "strong"
    return "weak" if pct < 0.5 else "neutral"


def evidence_text(clip: dict) -> str:
    pct = clip.get("source_pct")
    top = f", top {max(1, round((1 - pct) * 100))}% of recent viewer clips" if pct is not None else ""
    return f"{int(clip.get('source_views') or 0):,} views on Twitch{top}"


def _blocking(db: DB, clip_id: int | None) -> bool:
    """Does our overlapping clip still stand in the viewer clip's way?

    An AI-rejected cut of ours (often a weaker window of the same moment) doesn't:
    viewers clipping it is evidence the moment was good, so their cut gets a turn.
    Anything alive, or anything you rejected yourself, does.
    """
    row = db.one("SELECT status, decided_by FROM clips WHERE id = ?", (clip_id,)) if clip_id else None
    return bool(row) and not (row["status"] == "rejected" and row["decided_by"] == "auto")


def _overlap(db: DB, login: str, start_wall: float, window: float) -> int | None:
    """Id of the first of our clips near this moment that still blocks it, else None."""
    for r in db.query("SELECT id FROM clips WHERE channel = ? AND wall_start BETWEEN ? AND ? ORDER BY id",
                      (login, start_wall - window, start_wall + window)):
        if _blocking(db, r["id"]):
            return r["id"]
    return None


def harvest_once(db: DB, cfg: dict, helix: Helix | None = None, fetch=download) -> list[str]:
    tcfg = cfg["twitch_clips"]
    db.conn.executescript(SCHEMA)
    helix = helix or Helix()
    now = time.time()
    since = datetime.fromtimestamp(now - float(tcfg.get("lookback_hours", 3)) * 3600, timezone.utc)
    lines: list[str] = []
    for ch in cfg["channels"]:
        kind, _ = guardrails.permission(ch)
        if kind not in ("campaign", "written", "own") or ch.get("platform", "twitch") != "twitch":
            continue
        login = ch["login"].lower()
        try:
            bid = helix.broadcaster_id(login)
            clips = helix.clips(bid, since) if bid else []
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            lines.append(f"{login}: Twitch API error: {exc}")
            continue
        picked = 0
        min_views = int(ch.get("twitch_min_views", tcfg.get("min_views", 10)))  # small streams: their own bar
        pool = [int(x.get("view_count", 0)) for x in clips]
        hours = float(tcfg.get("lookback_hours", 3))
        for c in clips:
            if picked >= int(tcfg.get("per_poll", 3)) or c.get("view_count", 0) < min_views:
                break
            prev = db.one("SELECT note, clip_id FROM twitch_clips_seen WHERE id = ?", (c["id"],))
            if prev and not prev["note"].startswith("overlaps clip"):
                continue
            if prev and _blocking(db, prev["clip_id"] or int(prev["note"].rsplit(" ", 1)[-1])):
                continue  # our overlapping clip is still alive: nothing changed
            duration = float(c.get("duration") or 30)
            start_wall = _ts(c["created_at"]) - duration  # a clip covers the seconds before it was made

            def seen(note: str, clip_id: int | None = None) -> None:
                db.execute("INSERT OR REPLACE INTO twitch_clips_seen (id, channel, clip_id, views, note, seen_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (c["id"], login, clip_id, c.get("view_count", 0), note, now))

            if guardrails.before_release(cfg, {"wall_start": start_wall}):
                seen("pre-release")
                continue
            window = float(tcfg.get("dedupe_seconds", 30))
            dup = _overlap(db, login, start_wall, window)
            if dup:
                if not prev:
                    lines.append(f"{login}: '{c.get('title', '')}' overlaps our clip #{dup}, skipped")
                seen(f"overlaps clip {dup}", dup)
                continue
            path = config_mod.data_dir(cfg) / "raw" / login / f"twitch_{c['id'][:60]}.mp4"
            try:
                fetch(c, path)
            except Exception as exc:  # retried next poll (not marked seen)
                lines.append(f"{login}: download failed for '{c.get('title', '')}': {str(exc)[:120]}")
                continue
            clip_id = pipeline.queue_clip(db, cfg, channel=login, source=path, start_s=0.0,
                                          duration_s=min(duration, float(tcfg.get("max_seconds", 60))),
                                          score=score_for(c.get("view_count", 0)), wall_start=start_wall)
            views = int(c.get("view_count", 0))
            rank, pct = rank_in(views, pool, int(tcfg.get("min_pool", 5)))
            note = (f"Twitch viewers clipped this moment and titled it \"{c.get('title', '').strip()}\" "
                    f"({views:,} views on Twitch; #{rank} of the {len(pool)} clips viewers made "
                    f"in the last {hours:g}h).")
            db.update("clips", clip_id, source_note=note[:300], source_views=views, source_pct=pct)
            seen("queued", clip_id)
            picked += 1
            lines.append(f"{login}: queued clip #{clip_id} from viewer clip '{c.get('title', '')}' "
                         f"({c.get('view_count', 0)} views)")
    return lines


class ClipHarvester(threading.Thread):
    def __init__(self, db: DB, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name="twitch-clips")
        self.db, self.cfg, self.stop_event = db, cfg, stop

    def run(self) -> None:
        helix = Helix()
        interval = float(self.cfg["twitch_clips"].get("interval_minutes", 10)) * 60
        self.stop_event.wait(45)
        while not self.stop_event.is_set():
            try:
                for line in harvest_once(self.db, self.cfg, helix):
                    log.info("twitch clips: %s", line)
            except Exception:
                log.exception("twitch clip harvest failed")
            self.stop_event.wait(interval)
