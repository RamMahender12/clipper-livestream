"""SQLite state: recording sessions, chat spikes, clips, posts."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    segment_dir TEXT NOT NULL,
    title TEXT DEFAULT '',
    status TEXT DEFAULT 'recording'
);
CREATE TABLE IF NOT EXISTS spikes (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,
    channel TEXT NOT NULL,
    at REAL NOT NULL,
    score REAL NOT NULL,
    count INTEGER,
    baseline REAL,
    ratio REAL,
    z REAL,
    clip_mentions INTEGER,
    top_terms TEXT DEFAULT '',
    sample TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    spike_id INTEGER,
    channel TEXT NOT NULL,
    campaign_id TEXT DEFAULT '',
    source TEXT DEFAULT '',
    wall_start REAL,
    start_s REAL,
    duration_s REAL,
    score REAL DEFAULT 0,
    raw_path TEXT DEFAULT '',
    final_path TEXT DEFAULT '',
    transcript TEXT DEFAULT '',
    words_json TEXT DEFAULT '',
    hook TEXT DEFAULT '',
    title TEXT DEFAULT '',
    caption TEXT DEFAULT '',
    hashtags TEXT DEFAULT '',
    spoiler_risk TEXT DEFAULT 'unknown',
    quality INTEGER DEFAULT 0,
    ai_notes TEXT DEFAULT '',
    status TEXT DEFAULT 'queued',
    error TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    campaign_id TEXT DEFAULT '',
    remote_id TEXT DEFAULT '',
    url TEXT DEFAULT '',
    status TEXT DEFAULT '',
    error TEXT DEFAULT '',
    posted_at REAL,
    views INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    stats_at REAL,
    UNIQUE (clip_id, platform)
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS posts_platform ON posts(platform, posted_at);
"""

# Columns added after the first release; applied to old and new databases alike.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "clips": [
        ("decided_by", "TEXT DEFAULT ''"),  # '' | 'human' | 'auto'
        ("decision_note", "TEXT DEFAULT ''"),
        ("decided_at", "REAL"),
        ("auto_checked_at", "REAL"),
        ("expected_views", "INTEGER DEFAULT 0"),
    ],
}

# Clip lifecycle: queued -> rendering -> ready -> approved -> posted
#                                       \-> rejected      \-> failed
CLIP_STATUSES = ("queued", "rendering", "ready", "approved", "posted", "rejected", "failed")


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, **fields) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def update(self, table: str, row_id: int, **fields) -> None:
        if table == "clips":
            fields.setdefault("updated_at", time.time())
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), row_id))

    def claim_clip(self, from_status: str, to_status: str) -> sqlite3.Row | None:
        """Atomically move the best-scored clip from one status to another."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM clips WHERE status = ? ORDER BY score DESC, id ASC LIMIT 1",
                (from_status,),
            ).fetchone()
            if not row:
                return None
            self.conn.execute(
                "UPDATE clips SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (to_status, time.time(), row["id"], from_status),
            )
            self.conn.commit()
            return row


def open_db(cfg: dict) -> DB:
    from .config import data_dir

    return DB(data_dir(cfg) / "clipper.db")
