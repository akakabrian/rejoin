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
