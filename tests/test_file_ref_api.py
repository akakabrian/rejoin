from fastapi.testclient import TestClient

import rejoin.app as app_module
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
