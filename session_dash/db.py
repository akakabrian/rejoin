from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH, ensure_data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    cwd TEXT,
    started_at TEXT,
    last_activity TEXT,
    mtime REAL,
    size INTEGER,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    model TEXT,
    first_prompt TEXT,
    last_prompt TEXT,
    codex_summary TEXT,
    indexed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_last_activity ON sessions(last_activity DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd ON sessions(cwd);
CREATE INDEX IF NOT EXISTS idx_sessions_tool ON sessions(tool);

CREATE TABLE IF NOT EXISTS titles (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    generated_at TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pins (
    session_id TEXT PRIMARY KEY,
    pinned_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5(
    session_id UNINDEXED,
    first_prompt,
    last_prompt,
    codex_summary,
    title,
    tokenize='porter unicode61'
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def refresh_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM session_fts")
    conn.execute(
        """
        INSERT INTO session_fts (session_id, first_prompt, last_prompt, codex_summary, title)
        SELECT s.id,
               COALESCE(s.first_prompt, ''),
               COALESCE(s.last_prompt, ''),
               COALESCE(s.codex_summary, ''),
               COALESCE(t.title, '')
        FROM sessions s
        LEFT JOIN titles t ON t.session_id = s.id
        """
    )
    conn.commit()
