from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .config import CLAUDE_PROJECTS_ROOT, CODEX_SESSIONS_ROOT
from .db import connect, init_db, refresh_fts, transaction


@dataclass
class SessionRecord:
    id: str
    tool: str
    path: str
    cwd: str | None = None
    started_at: str | None = None
    last_activity: str | None = None
    mtime: float = 0.0
    size: int = 0
    message_count: int = 0
    tool_call_count: int = 0
    model: str | None = None
    first_prompt: str | None = None
    last_prompt: str | None = None
    codex_summary: str | None = None

    def to_row(self) -> dict:
        return {
            **self.__dict__,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") in ("text", "input_text", "output_text"):
                    parts.append(p.get("text", ""))
        return "\n".join(parts)
    return ""


def parse_claude_session(path: Path) -> SessionRecord | None:
    stat = path.stat()
    rec = SessionRecord(
        id=path.stem,
        tool="claude",
        path=str(path),
        mtime=stat.st_mtime,
        size=stat.st_size,
        last_activity=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    )

    found_first_user = False
    for evt in _iter_jsonl(path):
        et = evt.get("type")
        if not rec.started_at and evt.get("timestamp"):
            rec.started_at = evt["timestamp"]
        if not rec.cwd and evt.get("cwd"):
            rec.cwd = evt["cwd"]

        if et == "user":
            msg = evt.get("message", {})
            rec.message_count += 1
            if not found_first_user:
                text = _text_of(msg.get("content", ""))
                if text:
                    rec.first_prompt = text
                    found_first_user = True
        elif et == "assistant":
            msg = evt.get("message", {}) or {}
            rec.message_count += 1
            if msg.get("model") and not rec.model:
                rec.model = msg["model"]
            for part in msg.get("content", []) or []:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    rec.tool_call_count += 1
        elif et == "last-prompt":
            rec.last_prompt = evt.get("lastPrompt")

    if rec.first_prompt and not rec.last_prompt:
        rec.last_prompt = rec.first_prompt
    return rec


def parse_codex_session(path: Path) -> SessionRecord | None:
    stat = path.stat()
    rec = SessionRecord(
        id="",
        tool="codex",
        path=str(path),
        mtime=stat.st_mtime,
        size=stat.st_size,
        last_activity=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    )

    last_user_text: str | None = None
    first_user_text: str | None = None
    latest_summary: str | None = None

    for evt in _iter_jsonl(path):
        et = evt.get("type")
        ts = evt.get("timestamp")
        payload = evt.get("payload", {}) or {}

        if et == "session_meta":
            rec.id = payload.get("id", "") or rec.id
            rec.started_at = payload.get("timestamp") or ts
            rec.cwd = payload.get("cwd")
        elif et == "turn_context":
            model = payload.get("model") or payload.get("cfg", {}).get("model")
            if model and not rec.model:
                rec.model = model
        elif et == "response_item":
            item = payload
            if item.get("type") == "message":
                role = item.get("role")
                text = _text_of(item.get("content", []))
                if role == "user" and text:
                    rec.message_count += 1
                    if first_user_text is None:
                        first_user_text = text
                    last_user_text = text
                elif role == "assistant" and text:
                    rec.message_count += 1
            elif item.get("type") in ("function_call", "tool_use", "local_shell_call"):
                rec.tool_call_count += 1
        elif et == "compacted":
            hist = payload.get("replacement_history") or []
            for h in hist:
                if h.get("role") == "assistant":
                    txt = _text_of(h.get("content", []))
                    if txt:
                        latest_summary = txt
                if h.get("role") == "user" and first_user_text is None:
                    txt = _text_of(h.get("content", []))
                    if txt:
                        first_user_text = txt

    if not rec.id:
        # fall back to filename uuid
        stem = path.stem  # rollout-<ts>-<uuid>
        parts = stem.split("-")
        if len(parts) >= 6:
            rec.id = "-".join(parts[-5:])
        else:
            rec.id = stem
    rec.first_prompt = first_user_text
    rec.last_prompt = last_user_text or first_user_text
    rec.codex_summary = latest_summary
    return rec


def iter_claude_paths() -> Iterable[Path]:
    if not CLAUDE_PROJECTS_ROOT.exists():
        return []
    return CLAUDE_PROJECTS_ROOT.glob("*/*.jsonl")


def iter_codex_paths() -> Iterable[Path]:
    if not CODEX_SESSIONS_ROOT.exists():
        return []
    return CODEX_SESSIONS_ROOT.glob("**/rollout-*.jsonl")


def upsert(conn, rec: SessionRecord) -> None:
    row = rec.to_row()
    conn.execute(
        """
        INSERT INTO sessions (id, tool, path, cwd, started_at, last_activity, mtime, size,
                              message_count, tool_call_count, model, first_prompt, last_prompt,
                              codex_summary, indexed_at)
        VALUES (:id, :tool, :path, :cwd, :started_at, :last_activity, :mtime, :size,
                :message_count, :tool_call_count, :model, :first_prompt, :last_prompt,
                :codex_summary, :indexed_at)
        ON CONFLICT(id) DO UPDATE SET
            path = excluded.path,
            cwd = excluded.cwd,
            started_at = excluded.started_at,
            last_activity = excluded.last_activity,
            mtime = excluded.mtime,
            size = excluded.size,
            message_count = excluded.message_count,
            tool_call_count = excluded.tool_call_count,
            model = excluded.model,
            first_prompt = excluded.first_prompt,
            last_prompt = excluded.last_prompt,
            codex_summary = excluded.codex_summary,
            indexed_at = excluded.indexed_at
        """,
        row,
    )


def reindex(force: bool = False) -> dict:
    init_db()
    stats = {"claude_new": 0, "claude_updated": 0, "claude_skipped": 0,
             "codex_new": 0, "codex_updated": 0, "codex_skipped": 0, "errors": 0}

    with connect() as conn:
        existing = {
            row["path"]: (row["id"], row["mtime"] or 0.0)
            for row in conn.execute("SELECT id, path, mtime FROM sessions")
        }

        with transaction(conn):
            for path in iter_claude_paths():
                try:
                    st = path.stat()
                    prior = existing.get(str(path))
                    if prior and not force and abs(prior[1] - st.st_mtime) < 1e-6:
                        stats["claude_skipped"] += 1
                        continue
                    rec = parse_claude_session(path)
                    if rec is None:
                        continue
                    upsert(conn, rec)
                    if prior:
                        stats["claude_updated"] += 1
                    else:
                        stats["claude_new"] += 1
                except Exception:
                    stats["errors"] += 1

            for path in iter_codex_paths():
                try:
                    st = path.stat()
                    prior = existing.get(str(path))
                    if prior and not force and abs(prior[1] - st.st_mtime) < 1e-6:
                        stats["codex_skipped"] += 1
                        continue
                    rec = parse_codex_session(path)
                    if rec is None:
                        continue
                    upsert(conn, rec)
                    if prior:
                        stats["codex_updated"] += 1
                    else:
                        stats["codex_new"] += 1
                except Exception:
                    stats["errors"] += 1

        refresh_fts(conn)
    return stats


if __name__ == "__main__":
    import pprint
    pprint.pprint(reindex())
