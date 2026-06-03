import json

from fastapi.testclient import TestClient

import rejoin.app as app_module
import rejoin.brief as brief_module
from rejoin.db import connect, init_db, refresh_fts


def _build_db(path):
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO sessions
                (id, tool, path, cwd, first_prompt, last_activity)
            VALUES
                ('sess-1', 'codex', '/tmp/sess.jsonl', '/tmp/proj', 'work on files', '2026-01-01T00:00:00+00:00'),
                ('sess-2', 'claude', '/tmp/sess2.jsonl', '/tmp/proj', 'review markdown', '2026-01-02T00:00:00+00:00')
            """
        )
        conn.executemany(
            """
            INSERT INTO session_file_refs (
                session_id, provider, cwd, path_normalized, path_display,
                path_scope, basename, extension, operations_json,
                operation_summary, mention_count, max_confidence
            ) VALUES (
                :session_id, :provider, '/tmp/proj', :path, :path,
                'project', :basename, :extension, :operations_json,
                :operation_summary, :mention_count, :max_confidence
            )
            """,
            [
                {
                    "session_id": "sess-1",
                    "provider": "codex",
                    "path": "rejoin/app.py",
                    "basename": "app.py",
                    "extension": ".py",
                    "operations_json": '["edited", "read"]',
                    "operation_summary": "edited 1x, read 1x",
                    "mention_count": 2,
                    "max_confidence": 0.9,
                },
                {
                    "session_id": "sess-2",
                    "provider": "claude",
                    "path": "README.md",
                    "basename": "README.md",
                    "extension": ".md",
                    "operations_json": '["read"]',
                    "operation_summary": "read 1x",
                    "mention_count": 1,
                    "max_confidence": 0.7,
                },
            ],
        )
        conn.commit()
        refresh_fts(conn)


def _pin_session(path, session_id="sess-1"):
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO pins (session_id, pinned_at) VALUES (:id, '2026-01-01T00:10:00+00:00')",
            {"id": session_id},
        )
        conn.commit()


def _write_codex_transcript(path, session_id="sess-brief", cwd="/tmp/proj", extra_turns=None):
    events = [
        {
            "type": "session_meta",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "payload": {"id": session_id, "cwd": cwd},
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:01+00:00",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Build live session brief"}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:02+00:00",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Added the initial API route."}],
            },
        },
    ]
    events.extend(extra_turns or [])
    path.write_text("\n".join(json.dumps(event) for event in events))


def _build_brief_db(path, transcript_path, *, indexed_mtime=None):
    init_db(path)
    if indexed_mtime is None:
        indexed_mtime = transcript_path.stat().st_mtime
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                id, tool, path, cwd, first_prompt, last_prompt, codex_summary,
                last_activity, mtime, indexed_at
            ) VALUES (
                'sess-brief', 'codex', :path, '/tmp/proj',
                'Build live session brief', 'Add markdown output',
                'Implemented brief scaffolding and tests.',
                '2026-01-01T00:00:00+00:00', :mtime,
                '2026-01-01T00:05:00+00:00'
            )
            """,
            {"path": str(transcript_path), "mtime": indexed_mtime},
        )
        conn.execute(
            """
            INSERT INTO titles (session_id, title, content_hash)
            VALUES ('sess-brief', 'Live Session Brief MVP', 'hash')
            """
        )
        conn.execute(
            """
            INSERT INTO session_file_refs (
                session_id, provider, cwd, path_normalized, path_display,
                path_scope, basename, extension, operations_json,
                operation_summary, mention_count, max_confidence, exists_now
            ) VALUES (
                'sess-brief', 'codex', '/tmp/proj', 'rejoin/app.py', 'rejoin/app.py',
                'project', 'app.py', '.py', '["edited", "tested"]',
                'edited 1x, tested 1x', 2, 0.95, 1
            )
            """
        )
        conn.commit()


def test_changed_count_includes_all_providers():
    assert app_module._changed_count({
        "claude_new": 1,
        "codex_updated": 2,
        "hermes_new": 3,
        "opencode_updated": 4,
        "errors": 99,
    }) == 10


def test_api_session_files(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-1/files")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["files"][0]["path"] == "rejoin/app.py"
    assert data["files"][0]["operations"] == ["edited", "read"]


def test_api_files_search(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))

    client = TestClient(app_module.app)
    response = client.get("/api/files/search", params={"q": "rejoin/app.py"})

    assert response.status_code == 200
    data = response.json()
    assert data["files"][0]["session_id"] == "sess-1"
    assert data["files"][0]["path_normalized"] == "rejoin/app.py"


def test_api_files_search_filters_by_basename_ext_and_operation(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))

    client = TestClient(app_module.app)
    response = client.get(
        "/api/files/search",
        params={"basename": "app.py", "ext": "py", "operation": "edited"},
    )

    assert response.status_code == 200
    data = response.json()
    assert [f["path_normalized"] for f in data["files"]] == ["rejoin/app.py"]


def test_file_filtered_session_fragment(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/sessions", params={"file": "rejoin/app.py"})

    assert response.status_code == 200
    assert "work on files" in response.text
    assert "1 file" in response.text
    assert "review markdown" not in response.text


def test_session_fragment_supports_inline_file_filters(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/sessions", params={"q": "file:rejoin/app.py ext:py op:edited"})

    assert response.status_code == 200
    assert "work on files" in response.text
    assert "review markdown" not in response.text


def test_session_fragment_supports_inline_basename_with_text_query(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/sessions", params={"q": "work basename:app.py"})

    assert response.status_code == 200
    assert "app.py" in response.text
    assert "review markdown" not in response.text


def test_session_fragment_file_filter_bare_filename_matches_basename(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/sessions", params={"q": "file:app.py"})

    assert response.status_code == 200
    assert "work on files" in response.text
    assert "review markdown" not in response.text


def test_api_sessions_json_uses_same_filters_and_inline_query(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    _pin_session(db, "sess-1")
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: {"sess-1"})

    client = TestClient(app_module.app)
    response = client.get(
        "/api/sessions",
        params={"q": 'file:app.py active:true pinned:true tool:codex cwd:"/tmp/proj"'},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert [session["id"] for session in data["sessions"]] == ["sess-1"]
    assert data["sessions"][0]["active"] is True
    assert data["sessions"][0]["pinned"] is True
    assert data["sessions"][0]["file_count"] == 1


def test_api_sessions_json_supports_offset_pagination(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    first = client.get("/api/sessions", params={"limit": 1, "offset": 0}).json()
    second = client.get("/api/sessions", params={"limit": 1, "offset": 1}).json()

    assert len(first["sessions"]) == 1
    assert len(second["sessions"]) == 1
    assert first["sessions"][0]["id"] != second["sessions"][0]["id"]


def test_api_session_json_normalizes_metadata(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    _pin_session(db, "sess-1")
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: {"sess-1"})

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-1")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert isinstance(data["warnings"], list)
    session = data["session"]
    assert isinstance(session["warnings"], list)
    assert session["id"] == "sess-1"
    assert session["tool"] == "codex"
    assert session["cwd"] == "/tmp/proj"
    assert session["pinned"] is True
    assert session["active"] is True
    assert session["file_count"] == 1


def test_api_session_json_warns_when_source_missing(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-1")

    assert response.status_code == 200
    data = response.json()
    assert "source_path_missing" in data["warnings"]
    assert "freshness_unknown" in data["warnings"]


def test_api_session_brief_completed_session(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["generated_at"]
    assert data["warnings"] == []
    assert data["summary"]["overall"].startswith("Title: Live Session Brief MVP")
    assert data["freshness"]["active"] is False
    assert data["freshness"]["indexed_is_stale"] is False
    assert data["freshness"]["freshness_known"] is True
    assert data["freshness"]["tail_is_live"] is True
    assert data["freshness"]["tail_source"] == "live"
    assert data["freshness"]["summary_source"] == "indexed_extract+live_tail"
    assert data["freshness"]["files_source"] == "session_file_refs"
    assert data["freshness"]["file_ref_count"] == 1
    assert data["freshness"]["files_may_be_stale"] is False
    assert data["freshness"]["turn_count"] == 2
    assert data["freshness"]["turn_count_source"] == "live"
    assert data["freshness"]["last_indexed_at"] == "2026-01-01T00:05:00+00:00"
    assert data["latest_user_turn"]["text"] == "Build live session brief"
    assert data["latest_assistant_turn"]["text"] == "Added the initial API route."


def test_api_session_brief_active_stale_session_live_reads(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(
        transcript,
        extra_turns=[
            {
                "type": "response_item",
                "timestamp": "2026-01-01T00:00:03+00:00",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Fresh tail turn from disk."}],
                },
            }
        ],
    )
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript, indexed_mtime=transcript.stat().st_mtime - 10)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: {"sess-brief"})

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief", params={"fresh": "true"})

    assert response.status_code == 200
    data = response.json()
    assert data["freshness"]["active"] is True
    assert data["freshness"]["indexed_is_stale"] is True
    assert "indexed_stale" in data["warnings"]
    assert "file_refs_may_be_stale" in data["warnings"]
    assert data["freshness"]["files_may_be_stale"] is True
    assert data["freshness"]["turn_count"] == 3
    assert data["tail"][-1]["text"] == "Fresh tail turn from disk."


def test_api_session_brief_markdown_format(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief", params={"format": "markdown"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "# Session Brief: sess-brief" in response.text
    assert "## Progress" in response.text
    assert "- rejoin/app.py" in response.text
    assert "### 0 · user" in response.text
    assert "```text" in response.text


def test_api_session_brief_tail_limit(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(
        transcript,
        extra_turns=[
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Final question"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Final answer"}],
                },
            },
        ],
    )
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief", params={"tail": 1})

    assert response.status_code == 200
    data = response.json()
    assert len(data["tail"]) == 1
    assert data["tail"][0]["text"] == "Final answer"


def test_api_session_brief_includes_file_refs(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief")

    assert response.status_code == 200
    data = response.json()
    assert data["files"][0]["path"] == "rejoin/app.py"
    assert data["files"][0]["operations"] == ["edited", "tested"]
    assert "rejoin/app.py" in data["summary"]["progress"]
    assert "File operation signals: edited, tested." in data["summary"]["progress"]


def test_api_session_brief_virtual_source_freshness_unknown(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    init_db(db)
    with connect(db) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                id, tool, path, cwd, first_prompt, codex_summary,
                last_activity, mtime, indexed_at
            ) VALUES (
                'virtual-1', 'pi', 'agent-sessions://pi/virtual-1', '/tmp/proj',
                'Virtual source session', 'Indexed virtual summary',
                '2026-01-01T00:00:00+00:00', 123.0,
                '2026-01-01T00:05:00+00:00'
            )
            """
        )
        conn.commit()
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())
    monkeypatch.setattr(brief_module, "load_turns", lambda tool, path: [])

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/virtual-1/brief")

    assert response.status_code == 200
    data = response.json()
    assert data["freshness"]["source_mtime"] is None
    assert data["freshness"]["source_path_exists"] is None
    assert data["freshness"]["indexed_is_stale"] is None
    assert data["freshness"]["freshness_known"] is False
    assert "freshness_unknown" in data["warnings"]
    assert data["freshness"]["file_ref_count"] == 0
    assert data["freshness"]["files_source"] == "session_file_refs"
    assert data["freshness"]["files_may_be_stale"] is False


def test_api_session_brief_live_read_failure_falls_back(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    def fail_load_turns(tool, path):
        raise OSError("transcript unavailable")

    monkeypatch.setattr(brief_module, "load_turns", fail_load_turns)

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief")

    assert response.status_code == 200
    data = response.json()
    assert data["tail_error"] == "transcript unavailable"
    assert "tail_read_failed" in data["warnings"]
    assert data["freshness"]["tail_is_live"] is False
    assert data["freshness"]["tail_source"] == "indexed_fallback"
    assert data["freshness"]["summary_source"] == "indexed_extract+fallback_tail"
    assert data["freshness"]["turn_count_source"] == "indexed_fallback"
    assert data["summary"]["overall"].startswith("Title: Live Session Brief MVP")
    assert data["files"][0]["path"] == "rejoin/app.py"
    assert data["tail"][-1]["text"] == "Add markdown output"


def test_api_session_brief_last_turn_metadata(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(
        transcript,
        extra_turns=[
            {
                "type": "response_item",
                "timestamp": "2026-01-01T00:00:04+00:00",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Last visible turn"}],
                },
            }
        ],
    )
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief")

    assert response.status_code == 200
    data = response.json()
    assert data["freshness"]["last_turn_index"] == 2
    assert data["freshness"]["last_turn_role"] == "assistant"
    assert data["freshness"]["last_turn_at"] == "2026-01-01T00:00:04+00:00"


def test_api_session_brief_files_may_be_stale_when_source_mtime_differs(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript, indexed_mtime=transcript.stat().st_mtime - 60)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief")

    assert response.status_code == 200
    data = response.json()
    assert data["freshness"]["indexed_is_stale"] is True
    assert data["freshness"]["file_ref_count"] == 1
    assert data["freshness"]["files_may_be_stale"] is True


def test_api_session_brief_includes_source_fields(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief")

    assert response.status_code == 200
    data = response.json()
    assert data["generated_at"]
    assert data["freshness"]["source_path_exists"] is True
    assert data["freshness"]["tail_source"] == "live"
    assert data["freshness"]["turn_count_source"] == "live"
    assert data["freshness"]["files_source"] == "session_file_refs"
    assert data["freshness"]["file_ref_count"] == 1


def test_api_session_brief_markdown_has_freshness_note(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief", params={"format": "markdown"})

    assert response.status_code == 200
    assert "Freshness: indexed metadata matches the backing transcript mtime." in response.text


def test_api_reindex_supports_force(monkeypatch):
    calls = []

    def fake_reindex(force=False):
        calls.append(force)
        return {"errors": 0}

    async def fake_titles():
        return {"updated": 0}

    monkeypatch.setattr(app_module, "reindex", fake_reindex)
    monkeypatch.setattr(app_module, "backfill_titles", fake_titles)

    client = TestClient(app_module.app)
    response = client.post("/reindex", params={"force": "true", "titles": "false"})

    assert response.status_code == 200
    assert response.json()["errors"] == 0
    assert calls == [True]


def test_api_session_file_refs_rebuild(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-rebuild.jsonl"
    _write_codex_transcript(
        transcript,
        extra_turns=[
            {
                "type": "response_item",
                "timestamp": "2026-01-01T00:00:03+00:00",
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "arguments": json.dumps({"cmd": "sed -n '1,20p' rejoin/app.py"}),
                },
            }
        ],
    )
    db = tmp_path / "index.db"
    init_db(db)
    with connect(db) as conn:
        conn.execute(
            """
            INSERT INTO sessions (id, tool, path, cwd, first_prompt, mtime)
            VALUES ('sess-brief', 'codex', :path, '/tmp/proj', 'Rebuild refs', :mtime)
            """,
            {"path": str(transcript), "mtime": transcript.stat().st_mtime},
        )
        conn.commit()
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.post("/api/sessions/sess-brief/file-refs/rebuild")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["event_count"] >= 1
    assert data["file_ref_count"] == 1
    with connect(db) as conn:
        row = conn.execute("SELECT path_normalized FROM session_file_refs").fetchone()
        assert row["path_normalized"] == "rejoin/app.py"


def test_api_session_file_refs_rebuild_failure_has_stable_error(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-rebuild.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(
        app_module,
        "load_turns",
        lambda tool, path: (_ for _ in ()).throw(OSError("cannot read transcript")),
    )

    client = TestClient(app_module.app)
    response = client.post("/api/sessions/sess-brief/file-refs/rebuild")

    assert response.status_code == 500
    data = response.json()
    assert data["ok"] is False
    assert data["error"] == "file_ref_rebuild_failed"
    assert data["detail"] == "cannot read transcript"
    assert "Traceback" not in response.text


def test_api_session_brief_tail_zero_has_no_tail_metadata(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/brief", params={"tail": 0})

    assert response.status_code == 200
    data = response.json()
    assert data["tail"] == []
    assert data["freshness"]["turn_count"] == 2
    assert data["freshness"]["last_turn_index"] is None
    assert data["freshness"]["last_turn_role"] is None
    assert data["latest_user_turn"]["text"] == "Build live session brief"
    assert data["latest_assistant_turn"]["text"] == "Added the initial API route."


def test_api_session_tail(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(
        transcript,
        extra_turns=[
            {
                "type": "response_item",
                "timestamp": "2026-01-01T00:00:03+00:00",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Tail question"}],
                },
            }
        ],
    )
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/tail", params={"tail": 1})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["generated_at"]
    assert data["tail_source"] == "live"
    assert data["turn_count"] == 3
    assert len(data["tail"]) == 1
    assert data["tail"][0]["text"] == "Tail question"


def test_api_session_tail_live_failure_uses_indexed_fallback(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())
    monkeypatch.setattr(
        app_module,
        "load_turns",
        lambda tool, path: (_ for _ in ()).throw(OSError("tail unavailable")),
    )

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/tail", params={"fresh": "true"})

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["generated_at"]
    assert data["tail_source"] == "indexed_fallback"
    assert data["tail_error"] == "tail unavailable"
    assert "tail_read_failed" in data["warnings"]
    assert data["tail"][0]["meta"] == {"source": "indexed:first_prompt"}
    assert data["tail"][0]["text"] == "Build live session brief"


def test_api_session_tail_zero(monkeypatch, tmp_path):
    transcript = tmp_path / "rollout-brief.jsonl"
    _write_codex_transcript(transcript)
    db = tmp_path / "index.db"
    _build_brief_db(db, transcript)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: set())

    client = TestClient(app_module.app)
    response = client.get("/api/sessions/sess-brief/tail", params={"tail": 0})

    assert response.status_code == 200
    data = response.json()
    assert data["generated_at"]
    assert data["turn_count"] == 2
    assert data["tail"] == []


def test_api_projects_summary(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))

    client = TestClient(app_module.app)
    response = client.get("/api/projects")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["projects"] == [
        {
            "cwd": "/tmp/proj",
            "label": "/tmp/proj",
            "session_count": 2,
            "active_count": 0,
            "file_ref_count": 2,
            "last_activity": "2026-01-02T00:00:00+00:00",
            "tools": ["claude", "codex"],
        }
    ]


def test_api_projects_active_count_uses_running_ids(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module, "_running_ids", lambda: {"sess-1"})

    client = TestClient(app_module.app)
    response = client.get("/api/projects")

    assert response.status_code == 200
    data = response.json()
    assert data["projects"][0]["active_count"] == 1


def test_api_diagnostics_counts(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    _build_db(db)
    monkeypatch.setattr(app_module, "connect", lambda: connect(db))

    client = TestClient(app_module.app)
    response = client.get("/api/diagnostics")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["schema"]["user_version"] >= 1
    assert data["sessions"]["count"] == 2
    assert data["sessions"]["by_provider"] == {"claude": 1, "codex": 1}
    assert data["file_refs"]["count"] == 2
    assert data["file_refs"]["by_provider"] == {"claude": 1, "codex": 1}
    assert data["providers"] == {"claude": 1, "codex": 1}
