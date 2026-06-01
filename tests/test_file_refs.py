import json

from rejoin.db import connect, init_db
from rejoin.file_refs import (
    aggregate_file_refs,
    extract_file_ref_events,
    normalize_path,
    replace_session_file_refs,
)
from rejoin.transcript import Turn


def test_normalize_absolute_path_under_cwd(tmp_path):
    cwd = tmp_path / "proj"
    path = cwd / "rejoin" / "app.py"
    path.parent.mkdir(parents=True)
    path.write_text("")

    normalized = normalize_path(str(path), str(cwd), home=str(tmp_path))

    assert normalized is not None
    assert normalized.normalized == "rejoin/app.py"
    assert normalized.display == "rejoin/app.py"
    assert normalized.scope == "project"
    assert normalized.exists_now == 1


def test_extracts_basic_paths_and_operations(tmp_path):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    turns = [
        Turn("user", "please inspect `rejoin/app.py:42`", {}),
        Turn("tool_use", json.dumps({"cmd": "pytest tests/test_api.py::test_file_filter"}), {"name": "shell"}),
        Turn("tool_result", 'Traceback\n  File "' + str(cwd / "rejoin/app.py") + '", line 188, in x', {}),
        Turn("tool_result", "--- a/rejoin/db.py\n+++ b/rejoin/db.py\n@@ -1 +1\n", {}),
    ]

    events = extract_file_ref_events("sess-1", "codex", str(cwd), turns, home=str(tmp_path))
    by_path = {(event.path_normalized, event.operation) for event in events}

    assert ("rejoin/app.py", "mentioned") in by_path
    assert ("tests/test_api.py", "tested") in by_path
    assert ("rejoin/app.py", "errored") in by_path
    assert ("rejoin/db.py", "edited") in by_path


def test_does_not_treat_urls_as_paths():
    turns = [Turn("assistant", "See https://example.com/rejoin/app.py for docs", {})]

    events = extract_file_ref_events("sess-1", "codex", "/tmp/proj", turns)

    assert events == []


def test_aggregate_summarizes_operations():
    turns = [
        Turn("tool_use", json.dumps({"cmd": "cat rejoin/app.py"}), {"name": "shell"}),
        Turn("tool_use", json.dumps({"cmd": "pytest rejoin/app.py"}), {"name": "shell"}),
    ]

    refs = aggregate_file_refs(extract_file_ref_events("sess-1", "codex", "/tmp/proj", turns))

    assert len(refs) == 1
    assert refs[0].path_normalized == "rejoin/app.py"
    assert refs[0].mention_count == 2
    assert refs[0].operations == ["tested", "read"]
    assert "tested 1x" in refs[0].operation_summary


def test_replace_session_file_refs_persists_events_and_aggregate(tmp_path):
    db = tmp_path / "index.db"
    init_db(db)
    events = extract_file_ref_events(
        "sess-1",
        "codex",
        "/tmp/proj",
        [Turn("tool_use", json.dumps({"cmd": "sed -n '1,20p' rejoin/app.py"}), {"name": "shell"})],
    )
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO sessions (id, tool, path) VALUES ('sess-1', 'codex', '/tmp/sess.jsonl')"
        )
        replace_session_file_refs(conn, "sess-1", events)
        conn.commit()
        aggregate = conn.execute("SELECT * FROM session_file_refs").fetchone()
        event_count = conn.execute("SELECT COUNT(*) FROM session_file_ref_events").fetchone()[0]

    assert aggregate["path_normalized"] == "rejoin/app.py"
    assert aggregate["operation_summary"] == "read 1x"
    assert event_count == 1
