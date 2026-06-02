import pytest

from rejoin.db import SCHEMA_VERSION, SchemaVersionMismatch, connect, init_db


def test_init_db_fresh_sets_version(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    with connect(db) as c:
        v = c.execute("PRAGMA user_version").fetchone()[0]
        assert v == SCHEMA_VERSION


def test_init_db_matching_version_is_idempotent(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    init_db(db)  # second call should not raise


def test_init_db_mismatching_version_raises(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    with connect(db) as c:
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        c.commit()
    with pytest.raises(SchemaVersionMismatch):
        init_db(db)


def test_init_db_migrates_v1_to_v2_additively(tmp_path):
    db = tmp_path / "test.db"
    with connect(db) as c:
        c.execute(
            """
            CREATE TABLE session_file_ref_events (
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
                line_start INTEGER,
                line_end INTEGER,
                command TEXT,
                excerpt TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        c.execute("PRAGMA user_version = 1")
        c.commit()

    init_db(db)

    with connect(db) as c:
        v = c.execute("PRAGMA user_version").fetchone()[0]
        cols = {row["name"] for row in c.execute("PRAGMA table_info(session_file_ref_events)")}
        assert v == SCHEMA_VERSION
        assert "exists_now" in cols
        assert "session_file_refs" in {
            row["name"]
            for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
