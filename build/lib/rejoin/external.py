"""Integration with Lars de Ridder's `agent-sessions` library.

We keep our own Claude and Codex parsers (they extract more detail like
tool_call_count, model, and Codex compaction summaries). For OpenCode and
Pi we delegate fully: they're less common and we'd rather use his
battle-tested providers than duplicate the work.

`agent-sessions` is MIT; see README for credit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from agent_sessions import RunnerType, SessionSummary, get_session_detail
from agent_sessions.providers.opencode import list_opencode_sessions
from agent_sessions.providers.pi import list_pi_sessions
from agent_sessions.running import (
    find_running_claude_sessions,
    find_running_codex_sessions,
    find_running_opencode_sessions,
    find_running_pi_sessions,
)

from .common import Tool, utcnow_iso
from .indexer import SessionRecord
from .transcript import Turn

# Only the tools we don't parse ourselves.
EXTERNAL_TOOLS: tuple[Tool, ...] = ("opencode", "pi")

_RUNNER_BY_TOOL: dict[Tool, RunnerType] = {
    "opencode": RunnerType.OPENCODE,
    "pi": RunnerType.PI,
}


def list_external_sessions(tool: Tool) -> list[SessionRecord]:
    """Return SessionRecords for opencode/pi via agent-sessions."""
    if tool == "opencode":
        summaries = list_opencode_sessions(limit=1000)
    elif tool == "pi":
        summaries = list_pi_sessions(limit=1000)
    else:
        raise ValueError(f"not an external tool: {tool}")
    return [_to_record(s, tool) for s in summaries]


def _to_record(s: SessionSummary, tool: Tool) -> SessionRecord:
    # agent-sessions doesn't expose file paths; use session id as the key
    # for the `path` column. It's opaque but unique and stable.
    return SessionRecord(
        id=s.id,
        tool=tool,
        path=f"agent-sessions://{tool}/{s.id}",
        cwd=s.directory,
        started_at=s.last_activity,
        last_activity=s.last_activity,
        mtime=_iso_to_epoch(s.last_activity),
        size=0,
        message_count=s.message_count,
        tool_call_count=0,
        model=None,
        first_prompt=s.first_prompt,
        last_prompt=s.last_prompt,
    )


def _iso_to_epoch(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def iter_external_turns(tool: Tool, session_id: str) -> Iterator[Turn]:
    detail = get_session_detail(session_id, _RUNNER_BY_TOOL[tool], limit=1000)
    if not detail:
        return
    for msg in detail.messages:
        if msg.role not in ("user", "assistant") or not msg.content:
            continue
        yield Turn(msg.role, msg.content, {"ts": msg.timestamp})


def running_session_ids() -> set[str]:
    """Union of session IDs currently running across all four tools."""
    out: set[str] = set()
    for fn in (
        find_running_claude_sessions,
        find_running_codex_sessions,
        find_running_opencode_sessions,
        find_running_pi_sessions,
    ):
        try:
            out |= fn()
        except Exception:
            pass
    return out
