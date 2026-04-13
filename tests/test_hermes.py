"""Synthetic Hermes SQLite fixture tests. We construct a minimal state.db
matching the schema documented at:
  https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage
"""
import sqlite3

from rejoin.hermes import iter_hermes_turns, list_hermes_sessions


def _build_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            finish_reason TEXT,
            reasoning TEXT
        );
    """)
    conn.execute("""
        INSERT INTO sessions (id, model, started_at, message_count,
                              tool_call_count, title)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ("sess-1", "claude-opus-4", 1_000_000_000.0, 2, 1, "Debug Webhook Retry Loop"))
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("sess-1", "user", "fix the retry loop", None, None, 1_000_000_001.0),
            ("sess-1", "assistant", "looking at it", None, None, 1_000_000_002.0),
            ("sess-1", "assistant", None,
             '[{"function": {"name": "bash", "arguments": "{\\"cmd\\": \\"ls\\"}"}}]',
             None, 1_000_000_003.0),
            ("sess-1", "tool", "file1\nfile2", None, "bash", 1_000_000_004.0),
            ("sess-1", "user", "thanks", None, None, 1_000_000_005.0),
        ],
    )
    conn.commit()
    conn.close()


def test_list_hermes_sessions(tmp_path):
    db = tmp_path / "state.db"
    _build_db(db)
    rows = list_hermes_sessions(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "sess-1"
    assert r["tool"] == "hermes"
    assert r["path"] == "hermes://sess-1"
    assert r["model"] == "claude-opus-4"
    assert r["first_prompt"] == "fix the retry loop"
    assert r["last_prompt"] == "thanks"
    assert r["native_title"] == "Debug Webhook Retry Loop"
    assert r["message_count"] == 2  # Hermes-provided count, not ours
    assert r["tool_call_count"] == 1


def test_iter_hermes_turns(tmp_path):
    db = tmp_path / "state.db"
    _build_db(db)
    turns = list(iter_hermes_turns("sess-1", db))
    roles = [t.role for t in turns]
    # user -> assistant (text) -> tool_use -> tool_result -> user
    assert roles.count("user") == 2
    assert roles.count("assistant") == 1
    assert roles.count("tool_use") == 1
    assert roles.count("tool_result") == 1
    tool_use = next(t for t in turns if t.role == "tool_use")
    assert tool_use.meta["name"] == "bash"
    assert "ls" in tool_use.text


def test_list_hermes_sessions_missing_db(tmp_path):
    missing = tmp_path / "nope.db"
    assert list_hermes_sessions(missing) == []
