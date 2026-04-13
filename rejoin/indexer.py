from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .common import Tool, iter_jsonl, text_of, utcnow_iso, uuid_from_stem
from .config import CLAUDE_PROJECTS_ROOT, CODEX_SESSIONS_ROOT, OPENCLAW_AGENTS_ROOT
from .db import connect, init_db, refresh_fts, transaction


@dataclass
class SessionRecord:
    id: str
    tool: Tool
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
        return {**self.__dict__, "indexed_at": utcnow_iso()}


def _stat_record(path: Path, tool: Tool, id_: str = "") -> SessionRecord:
    stat = path.stat()
    return SessionRecord(
        id=id_,
        tool=tool,
        path=str(path),
        mtime=stat.st_mtime,
        size=stat.st_size,
        last_activity=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
    )


def parse_claude_session(path: Path) -> SessionRecord | None:
    rec = _stat_record(path, "claude", id_=path.stem)
    found_first_user = False
    for evt in iter_jsonl(path):
        et = evt.get("type")
        if not rec.started_at and evt.get("timestamp"):
            rec.started_at = evt["timestamp"]
        if not rec.cwd and evt.get("cwd"):
            rec.cwd = evt["cwd"]

        if et == "user":
            msg = evt.get("message", {})
            rec.message_count += 1
            if not found_first_user:
                text = text_of(msg.get("content", ""))
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
    rec = _stat_record(path, "codex")

    first_user_text: str | None = None
    last_user_text: str | None = None
    latest_summary: str | None = None

    for evt in iter_jsonl(path):
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
            it_type = payload.get("type")
            if it_type == "message":
                role = payload.get("role")
                text = text_of(payload.get("content", []))
                if role == "user" and text:
                    rec.message_count += 1
                    if first_user_text is None:
                        first_user_text = text
                    last_user_text = text
                elif role == "assistant" and text:
                    rec.message_count += 1
            elif it_type in ("function_call", "tool_use", "local_shell_call"):
                rec.tool_call_count += 1
        elif et == "compacted":
            for h in payload.get("replacement_history") or []:
                if h.get("role") == "assistant":
                    txt = text_of(h.get("content", []))
                    if txt:
                        latest_summary = txt
                elif h.get("role") == "user" and first_user_text is None:
                    txt = text_of(h.get("content", []))
                    if txt:
                        first_user_text = txt

    if not rec.id:
        rec.id = uuid_from_stem(path.stem)
    rec.first_prompt = first_user_text
    rec.last_prompt = last_user_text or first_user_text
    rec.codex_summary = latest_summary
    return rec


def parse_openclaw_session(path: Path) -> SessionRecord | None:
    """OpenClaw uses Pi's JSONL format but nests sessions under
    ~/.openclaw/agents/<agentId>/sessions/<sessionId>.jsonl.
    Session header line: {"type": "session", "id": ..., "cwd": ...}.
    """
    rec = _stat_record(path, "openclaw", id_=path.stem)
    first_user_text: str | None = None
    last_user_text: str | None = None
    for evt in iter_jsonl(path):
        et = evt.get("type")
        ts = evt.get("timestamp")
        if et == "session":
            rec.id = evt.get("id") or rec.id
            rec.started_at = ts
            rec.cwd = evt.get("cwd")
            continue
        if et != "message":
            continue
        msg = evt.get("message", {}) or {}
        role = msg.get("role")
        text = text_of(msg.get("content", ""))
        if role == "user" and text:
            rec.message_count += 1
            if first_user_text is None:
                first_user_text = text
            last_user_text = text
        elif role == "assistant":
            rec.message_count += 1
            if msg.get("model") and not rec.model:
                rec.model = msg["model"]
            for part in msg.get("content", []) or []:
                if isinstance(part, dict) and part.get("type") == "toolCall":
                    rec.tool_call_count += 1
    if not rec.id:
        rec.id = uuid_from_stem(path.stem)
    rec.first_prompt = first_user_text
    rec.last_prompt = last_user_text or first_user_text
    return rec


PARSERS: dict[Tool, Callable[[Path], SessionRecord | None]] = {
    "claude": parse_claude_session,
    "codex": parse_codex_session,
    "openclaw": parse_openclaw_session,
}


def _iter_paths(tool: Tool) -> Iterable[Path]:
    if tool == "claude":
        return CLAUDE_PROJECTS_ROOT.glob("*/*.jsonl")
    if tool == "codex":
        return CODEX_SESSIONS_ROOT.glob("**/rollout-*.jsonl")
    if tool == "openclaw":
        return OPENCLAW_AGENTS_ROOT.glob("*/sessions/*.jsonl")
    return []


_UPSERT_COLUMNS = (
    "id", "tool", "path", "cwd", "started_at", "last_activity", "mtime", "size",
    "message_count", "tool_call_count", "model", "first_prompt", "last_prompt",
    "codex_summary", "indexed_at",
)
_UPSERT_SQL = (
    f"INSERT INTO sessions ({', '.join(_UPSERT_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _UPSERT_COLUMNS)}) "
    "ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _UPSERT_COLUMNS if c != "id")
)


def upsert(conn, rec: SessionRecord) -> None:
    conn.execute(_UPSERT_SQL, rec.to_row())


def reindex(force: bool = False) -> dict:
    init_db()
    stats = {"errors": 0}
    for tool in PARSERS:
        stats[f"{tool}_new"] = 0
        stats[f"{tool}_updated"] = 0
        stats[f"{tool}_skipped"] = 0

    with connect() as conn:
        existing = {
            row["path"]: (row["id"], row["mtime"] or 0.0)
            for row in conn.execute("SELECT id, path, mtime FROM sessions")
        }

        changed = 0
        with transaction(conn):
            for tool, parser in PARSERS.items():
                for path in _iter_paths(tool):
                    try:
                        st = path.stat()
                        prior = existing.get(str(path))
                        if prior and not force and abs(prior[1] - st.st_mtime) < 1e-6:
                            stats[f"{tool}_skipped"] += 1
                            continue
                        rec = parser(path)
                        if rec is None:
                            continue
                        upsert(conn, rec)
                        stats[f"{tool}_updated" if prior else f"{tool}_new"] += 1
                        changed += 1
                    except Exception:
                        stats["errors"] += 1

            # OpenCode + Pi come in via the agent-sessions library, which
            # returns summaries rather than file paths. Imported lazily so
            # the indexer can still run if the dep is missing.
            try:
                from .external import EXTERNAL_TOOLS, list_external_sessions
            except Exception:
                EXTERNAL_TOOLS = ()
                list_external_sessions = None

            for tool in EXTERNAL_TOOLS:
                stats.setdefault(f"{tool}_new", 0)
                stats.setdefault(f"{tool}_updated", 0)
                try:
                    for rec in list_external_sessions(tool):
                        prior = existing.get(rec.path)
                        if prior and not force and abs(prior[1] - rec.mtime) < 1e-6:
                            continue
                        upsert(conn, rec)
                        stats[f"{tool}_updated" if prior else f"{tool}_new"] += 1
                        changed += 1
                except Exception:
                    stats["errors"] += 1

            # Hermes uses a single SQLite DB (~/.hermes/state.db). Read it
            # directly and upsert each session. Native Hermes titles land in
            # the `titles` table so our OpenRouter titler doesn't regenerate.
            stats.setdefault("hermes_new", 0)
            stats.setdefault("hermes_updated", 0)
            try:
                from .hermes import list_hermes_sessions
                for raw in list_hermes_sessions():
                    native_title = raw.pop("native_title", None)
                    rec = SessionRecord(**{k: v for k, v in raw.items()
                                           if k != "indexed_at"})
                    prior = existing.get(rec.path)
                    upsert(conn, rec)
                    stats["hermes_updated" if prior else "hermes_new"] += 1
                    changed += 1
                    if native_title:
                        conn.execute(
                            """INSERT INTO titles
                                (session_id, title, content_hash, generated_at, tokens_in, tokens_out)
                                VALUES (:id, :t, 'hermes-native', :now, 0, 0)
                                ON CONFLICT(session_id) DO UPDATE SET
                                    title = excluded.title,
                                    content_hash = excluded.content_hash""",
                            {"id": rec.id, "t": native_title, "now": utcnow_iso()},
                        )
            except Exception:
                stats["errors"] += 1

        if changed:
            refresh_fts(conn)
    return stats


if __name__ == "__main__":
    import pprint
    pprint.pprint(reindex())
