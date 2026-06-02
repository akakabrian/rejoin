from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH, ensure_data_dir

SCHEMA_VERSION = 2

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

CREATE TABLE IF NOT EXISTS session_file_ref_events (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    cwd TEXT,
    turn_index INTEGER,
    event_index INTEGER,
    source_kind TEXT NOT NULL,
    path_raw TEXT NOT NULL,
    path_normalized TEXT NOT NULL,
    path_display TEXT NOT NULL,
    path_scope TEXT NOT NULL DEFAULT 'unknown',
    basename TEXT,
    extension TEXT,
    operation TEXT NOT NULL DEFAULT 'mentioned',
    confidence REAL NOT NULL DEFAULT 0.5,
    exists_now INTEGER,
    line_start INTEGER,
    line_end INTEGER,
    command TEXT,
    excerpt TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_file_ref_events_session
    ON session_file_ref_events(session_id);
CREATE INDEX IF NOT EXISTS idx_file_ref_events_path
    ON session_file_ref_events(path_normalized);
CREATE INDEX IF NOT EXISTS idx_file_ref_events_provider
    ON session_file_ref_events(provider);

CREATE TABLE IF NOT EXISTS session_file_refs (
    session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    cwd TEXT,
    path_normalized TEXT NOT NULL,
    path_display TEXT NOT NULL,
    path_scope TEXT NOT NULL DEFAULT 'unknown',
    basename TEXT,
    extension TEXT,
    operations_json TEXT NOT NULL DEFAULT '[]',
    operation_summary TEXT,
    mention_count INTEGER NOT NULL DEFAULT 0,
    first_turn_index INTEGER,
    last_turn_index INTEGER,
    max_confidence REAL NOT NULL DEFAULT 0.0,
    exists_now INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, path_normalized),
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_file_refs_path
    ON session_file_refs(path_normalized);
CREATE INDEX IF NOT EXISTS idx_file_refs_basename
    ON session_file_refs(basename);
CREATE INDEX IF NOT EXISTS idx_file_refs_extension
    ON session_file_refs(extension);
CREATE INDEX IF NOT EXISTS idx_file_refs_cwd
    ON session_file_refs(cwd);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


class SchemaVersionMismatch(RuntimeError):
    """Raised when the on-disk DB schema version doesn't match this build."""


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    # v2 keeps the file-reference schema additive and records per-event
    # existence checks, matching the already-aggregated session_file_refs row.
    conn.executescript(SCHEMA)
    if not _has_column(conn, "session_file_ref_events", "exists_now"):
        conn.execute("ALTER TABLE session_file_ref_events ADD COLUMN exists_now INTEGER")
    conn.execute("PRAGMA user_version = 2")


def init_db(path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        existing = conn.execute("PRAGMA user_version").fetchone()[0]
        if existing == 0:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        elif existing == 1:
            _migrate_v1_to_v2(conn)
            conn.commit()
        elif existing == SCHEMA_VERSION:
            # already on the current schema; re-run `IF NOT EXISTS` is safe
            # and lets us add new tables in place.
            conn.executescript(SCHEMA)
            conn.commit()
        else:
            raise SchemaVersionMismatch(
                f"rejoin DB at {path} is on schema v{existing}, "
                f"this build expects v{SCHEMA_VERSION}. "
                f"Back up or delete {path} to force a clean reindex."
            )


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
