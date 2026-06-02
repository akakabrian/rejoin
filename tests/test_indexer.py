import json
import sys
import types

from rejoin.db import connect, init_db
from rejoin.indexer import SessionRecord, parse_claude_session, parse_codex_session


def test_parse_claude_session_minimal(tmp_path):
    path = tmp_path / "abc-123.jsonl"
    events = [
        {"type": "user", "message": {"role": "user", "content": "hello"},
         "timestamp": "2026-04-01T00:00:00Z",
         "cwd": "/home/u/proj"},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-opus-4-6",
                     "content": [
                         {"type": "tool_use", "name": "Bash", "input": {}},
                         {"type": "text", "text": "ok"},
                     ]}},
        {"type": "last-prompt", "lastPrompt": "bye"},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events))
    rec = parse_claude_session(path)
    assert rec is not None
    assert rec.tool == "claude"
    assert rec.first_prompt == "hello"
    assert rec.last_prompt == "bye"
    assert rec.cwd == "/home/u/proj"
    assert rec.model == "claude-opus-4-6"
    assert rec.tool_call_count == 1
    assert rec.message_count == 2  # 1 user + 1 assistant


def test_parse_claude_session_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    rec = parse_claude_session(path)
    assert rec is not None
    assert rec.first_prompt is None


def test_parse_claude_session_malformed_lines(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n'
        'not-json\n'
        '{"type":"assistant","message":{"role":"assistant","content":[]}}\n'
    )
    rec = parse_claude_session(path)
    assert rec is not None
    assert rec.first_prompt == "hi"
    assert rec.message_count == 2


def test_parse_codex_session_minimal(tmp_path):
    path = tmp_path / "rollout-2026-04-07-abc.jsonl"
    events = [
        {"type": "session_meta",
         "timestamp": "2026-04-07T21:02:54Z",
         "payload": {"id": "019d69c1-abcd-efff-0000-111122223333",
                     "timestamp": "2026-04-07T21:02:54Z",
                     "cwd": "/home/u/proj"}},
        {"type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hi there"}]}},
        {"type": "response_item",
         "payload": {"type": "function_call", "name": "shell",
                     "arguments": "{}"}},
        {"type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "ack"}]}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events))
    rec = parse_codex_session(path)
    assert rec is not None
    assert rec.tool == "codex"
    assert rec.id == "019d69c1-abcd-efff-0000-111122223333"
    assert rec.cwd == "/home/u/proj"
    assert rec.first_prompt == "hi there"
    assert rec.tool_call_count == 1
    assert rec.message_count == 2


def test_parse_openclaw_session(tmp_path):
    from rejoin.indexer import parse_openclaw_session
    path = tmp_path / "abc-123.jsonl"
    events = [
        {"type": "session", "id": "abc-123",
         "cwd": "/home/u/proj", "timestamp": "2026-04-01T00:00:00Z"},
        {"type": "message", "timestamp": "2026-04-01T00:00:05Z",
         "message": {"role": "user", "content": "build me a thing"}},
        {"type": "message", "timestamp": "2026-04-01T00:00:10Z",
         "message": {"role": "assistant", "model": "claude-opus-4",
                     "content": [
                         {"type": "toolCall", "name": "shell", "input": {}},
                         {"type": "text", "text": "on it"},
                     ]}},
        {"type": "message", "timestamp": "2026-04-01T00:01:00Z",
         "message": {"role": "user", "content": "thanks"}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events))
    rec = parse_openclaw_session(path)
    assert rec is not None
    assert rec.tool == "openclaw"
    assert rec.id == "abc-123"
    assert rec.cwd == "/home/u/proj"
    assert rec.first_prompt == "build me a thing"
    assert rec.last_prompt == "thanks"
    assert rec.model == "claude-opus-4"
    assert rec.tool_call_count == 1
    assert rec.message_count == 3


def test_parse_codex_session_recovers_id_from_filename(tmp_path):
    path = tmp_path / "rollout-2026-04-07T11-02-54-019d69c1-6142-7670-966f-61d8d2684158.jsonl"
    path.write_text('{"type":"response_item","payload":{"type":"message","role":"user","content":[]}}\n')
    rec = parse_codex_session(path)
    assert rec is not None
    assert rec.id == "019d69c1-6142-7670-966f-61d8d2684158"


def _patch_reindex_db(monkeypatch, db):
    import rejoin.indexer as indexer

    monkeypatch.setattr(indexer, "init_db", lambda: init_db(db))
    monkeypatch.setattr(indexer, "connect", lambda: connect(db))
    return indexer


def test_reindex_file_ref_extraction_failure_is_nonfatal(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    session = tmp_path / "rollout-2026-04-07-abc.jsonl"
    session.write_text(
        "\n".join([
            json.dumps({
                "type": "session_meta",
                "payload": {"id": "sess-1", "cwd": str(tmp_path)},
            }),
            json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "edit rejoin/app.py"}],
                },
            }),
        ])
    )
    indexer = _patch_reindex_db(monkeypatch, db)
    monkeypatch.setattr(indexer, "PARSERS", {"codex": parse_codex_session})
    monkeypatch.setattr(indexer, "_iter_paths", lambda tool: [session])
    monkeypatch.setattr(
        indexer,
        "_replace_file_refs_for_record",
        lambda conn, rec: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setitem(
        sys.modules,
        "rejoin.external",
        types.SimpleNamespace(EXTERNAL_TOOLS=(), list_external_sessions=lambda tool: []),
    )
    monkeypatch.setitem(
        sys.modules,
        "rejoin.hermes",
        types.SimpleNamespace(list_hermes_sessions=lambda: []),
    )

    stats = indexer.reindex()

    assert stats["codex_new"] == 1
    assert stats["file_ref_errors"] == 1
    assert stats["errors"] == 0
    with connect(db) as conn:
        assert conn.execute("SELECT id FROM sessions").fetchone()["id"] == "sess-1"


def test_reindex_counts_external_mtime_skips(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    init_db(db)
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO sessions (id, tool, path, mtime) VALUES ('ext-1', 'opencode', 'agent-sessions://opencode/ext-1', 222.0)"
        )
        conn.commit()
    indexer = _patch_reindex_db(monkeypatch, db)
    monkeypatch.setattr(indexer, "PARSERS", {})
    monkeypatch.setitem(
        sys.modules,
        "rejoin.external",
        types.SimpleNamespace(
            EXTERNAL_TOOLS=("opencode",),
            list_external_sessions=lambda tool: [
                SessionRecord(
                    id="ext-1",
                    tool="opencode",
                    path="agent-sessions://opencode/ext-1",
                    mtime=222.0,
                )
            ],
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "rejoin.hermes",
        types.SimpleNamespace(list_hermes_sessions=lambda: []),
    )

    stats = indexer.reindex()

    assert stats["opencode_skipped"] == 1
    assert stats["opencode_updated"] == 0


def test_reindex_counts_hermes_mtime_skips(monkeypatch, tmp_path):
    db = tmp_path / "index.db"
    init_db(db)
    with connect(db) as conn:
        conn.execute(
            "INSERT INTO sessions (id, tool, path, mtime) VALUES ('h1', 'hermes', 'hermes://h1', 333.0)"
        )
        conn.commit()
    indexer = _patch_reindex_db(monkeypatch, db)
    monkeypatch.setattr(indexer, "PARSERS", {})
    monkeypatch.setitem(
        sys.modules,
        "rejoin.external",
        types.SimpleNamespace(EXTERNAL_TOOLS=(), list_external_sessions=lambda tool: []),
    )
    monkeypatch.setitem(
        sys.modules,
        "rejoin.hermes",
        types.SimpleNamespace(
            list_hermes_sessions=lambda: [
                {
                    "id": "h1",
                    "tool": "hermes",
                    "path": "hermes://h1",
                    "cwd": None,
                    "started_at": None,
                    "last_activity": None,
                    "mtime": 333.0,
                    "size": 0,
                    "message_count": 0,
                    "tool_call_count": 0,
                    "model": None,
                    "first_prompt": None,
                    "last_prompt": None,
                    "codex_summary": None,
                    "native_title": None,
                }
            ]
        ),
    )

    stats = indexer.reindex()

    assert stats["hermes_skipped"] == 1
    assert stats["hermes_updated"] == 0
