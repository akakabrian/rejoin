"""Hermes Agent provider. Reads ~/.hermes/state.db directly.

Hermes (Nous Research) stores all sessions in one SQLite DB:
- sessions(id, model, started_at, message_count, tool_call_count, title, ...)
- messages(id, session_id, role, content, tool_calls, timestamp, ...)

Schema docs: https://hermes-agent.nousresearch.com/docs/developer-guide/session-storage
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from .common import utcnow_iso
from .config import HERMES_DB_PATH
from .transcript import Turn


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _epoch_to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), UTC).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def list_hermes_sessions(db_path: Path = HERMES_DB_PATH) -> list[dict]:
    """Return session records; shape compatible with SessionRecord fields."""
    if not db_path.exists():
        return []
    out: list[dict] = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.model, s.started_at, s.ended_at,
                   s.message_count, s.tool_call_count, s.title,
                   (SELECT content FROM messages
                      WHERE session_id = s.id AND role = 'user'
                      ORDER BY timestamp ASC LIMIT 1) AS first_prompt,
                   (SELECT content FROM messages
                      WHERE session_id = s.id AND role = 'user'
                      ORDER BY timestamp DESC LIMIT 1) AS last_prompt,
                   (SELECT MAX(timestamp) FROM messages
                      WHERE session_id = s.id) AS last_msg_ts
            FROM sessions s
            ORDER BY s.started_at DESC
            """
        ).fetchall()
    stat = db_path.stat()
    for r in rows:
        started = _epoch_to_iso(r["started_at"])
        last_act = _epoch_to_iso(r["last_msg_ts"] or r["started_at"])
        out.append({
            "id": r["id"],
            "tool": "hermes",
            "path": f"hermes://{r['id']}",
            "cwd": None,
            "started_at": started,
            "last_activity": last_act,
            "mtime": stat.st_mtime,  # shared DB mtime — not per-session
            "size": 0,
            "message_count": r["message_count"] or 0,
            "tool_call_count": r["tool_call_count"] or 0,
            "model": r["model"],
            "first_prompt": r["first_prompt"],
            "last_prompt": r["last_prompt"],
            "codex_summary": None,
            "native_title": r["title"],
            "indexed_at": utcnow_iso(),
        })
    return out


def iter_hermes_turns(session_id: str,
                      db_path: Path = HERMES_DB_PATH) -> Iterator[Turn]:
    if not db_path.exists():
        return
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT role, content, tool_calls, tool_name, timestamp
            FROM messages
            WHERE session_id = :id
            ORDER BY timestamp ASC
            """,
            {"id": session_id},
        ).fetchall()
    for r in rows:
        role = r["role"]
        ts = _epoch_to_iso(r["timestamp"])
        if role in ("user", "assistant") and r["content"]:
            yield Turn(role, r["content"], {"ts": ts})
        if r["tool_calls"]:
            try:
                tcs = json.loads(r["tool_calls"])
            except (ValueError, TypeError):
                tcs = []
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    fn = (tc.get("function") or {})
                    args = fn.get("arguments") or tc.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            pass
                    name = fn.get("name") or tc.get("name") or r["tool_name"]
                    body = json.dumps(args, indent=2) if not isinstance(args, str) else args
                    yield Turn("tool_use", body[:4000], {"name": name, "ts": ts})
        if role == "tool" and r["content"]:
            yield Turn("tool_result", str(r["content"])[:4000],
                       {"name": r["tool_name"], "ts": ts})
