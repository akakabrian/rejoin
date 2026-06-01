from __future__ import annotations

import json
import re
import shlex
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .common import Tool, utcnow_iso
from .transcript import Turn

OPERATIONS = {
    "mentioned",
    "read",
    "searched",
    "edited",
    "created",
    "deleted",
    "renamed",
    "tested",
    "errored",
    "committed",
    "unknown",
}

_PATHISH_RE = re.compile(
    r"(?P<path>"
    r"(?:~|\.{1,2}|/)[A-Za-z0-9_./@+\-= ]+\.[A-Za-z0-9_+\-=]+"
    r"|[A-Za-z0-9_./@+\-=]+/[A-Za-z0-9_./@+\-=]*\.[A-Za-z0-9_+\-=]+"
    r"|(?:README|CHANGELOG|LICENSE|Makefile|Dockerfile)(?:\.[A-Za-z0-9_+\-=]+)?"
    r"|[A-Za-z0-9_+\-=]+\.(?:py|js|ts|tsx|jsx|html|css|md|toml|yaml|yml|json|sql|sh|txt|rst|cfg|ini)"
    r")"
    r"(?::(?P<line>\d+)(?:-\d+)?)?"
    r"(?:\:\:[A-Za-z_][A-Za-z0-9_]*)?"
)
_STACK_RE = re.compile(r'File "([^"]+)", line (\d+)')
_DIFF_RE = re.compile(r"^(?:---|\+\+\+) [ab]/(.+)$", re.MULTILINE)
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SHELL_REDIRECT_RE = re.compile(r"(?:^|\s)(?:>|>>)\s*([^\s]+)")


@dataclass(frozen=True)
class NormalizedPath:
    raw: str
    normalized: str
    display: str
    scope: str
    basename: str
    extension: str
    exists_now: int | None


@dataclass(frozen=True)
class FileRefEvent:
    session_id: str
    provider: Tool
    cwd: str | None
    turn_index: int | None
    event_index: int
    source_kind: str
    path_raw: str
    path_normalized: str
    path_display: str
    path_scope: str
    basename: str
    extension: str
    operation: str
    confidence: float
    line_start: int | None = None
    line_end: int | None = None
    command: str | None = None
    excerpt: str | None = None
    exists_now: int | None = None


@dataclass(frozen=True)
class SessionFileRef:
    session_id: str
    provider: Tool
    cwd: str | None
    path_normalized: str
    path_display: str
    path_scope: str
    basename: str
    extension: str
    operations: list[str]
    operation_summary: str
    mention_count: int
    first_turn_index: int | None
    last_turn_index: int | None
    max_confidence: float
    exists_now: int | None


def _clean_path(raw: str) -> str:
    path = raw.strip().strip("`'\"()[]{}<>,")
    path = re.sub(r"(?<!:):\d+(?:-\d+)?$", "", path)
    path = re.sub(r"::[A-Za-z_][A-Za-z0-9_]*$", "", path)
    return path


def normalize_path(raw: str, cwd: str | None, home: str | None = None) -> NormalizedPath | None:
    raw = _clean_path(raw)
    if not raw or _URL_RE.match(raw) or raw.startswith("mailto:"):
        return None
    if raw.startswith(("hermes://", "agent-sessions://")):
        return NormalizedPath(raw, raw, raw, "virtual", Path(raw).name, "", None)

    home_path = Path(home).expanduser() if home else Path.home()
    cwd_path = Path(cwd).expanduser().resolve() if cwd else None
    expanded = raw
    if raw.startswith("~/"):
        expanded = str(home_path / raw[2:])

    is_abs = expanded.startswith("/")
    candidate = Path(expanded)
    absolute = candidate if is_abs else ((cwd_path / candidate) if cwd_path else None)

    exists_now: int | None = None
    if absolute:
        try:
            exists_now = 1 if absolute.exists() else 0
        except OSError:
            exists_now = None

    if absolute and cwd_path:
        try:
            rel = absolute.resolve().relative_to(cwd_path)
            display = rel.as_posix()
            scope = "project"
            normalized = display
        except (OSError, ValueError):
            display = str(candidate) if not is_abs else str(absolute)
            scope = "outside_project" if is_abs else "unknown"
            normalized = display
    elif is_abs:
        display = str(candidate)
        scope = "absolute"
        normalized = display
    else:
        display = str(candidate).removeprefix("./")
        scope = "unknown"
        normalized = display

    normalized = normalized.replace("\\", "/")
    display = display.replace("\\", "/")
    basename = Path(display).name
    extension = Path(basename).suffix
    return NormalizedPath(raw, normalized, display, scope, basename, extension, exists_now)


def _path_candidates(text: str) -> list[tuple[str, int | None, int | None]]:
    out: list[tuple[str, int | None, int | None]] = []
    for match in _STACK_RE.finditer(text):
        out.append((match.group(1), int(match.group(2)), int(match.group(2))))
    for match in _DIFF_RE.finditer(text):
        path = match.group(1)
        if path != "/dev/null":
            out.append((path, None, None))
    for match in _PATHISH_RE.finditer(text):
        window = text[max(0, match.start() - 12):match.end()]
        if "://" in window:
            continue
        line = int(match.group("line")) if match.group("line") else None
        out.append((match.group("path"), line, line))
    return out


def _command_operation(command: str, path: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    if not words:
        return "mentioned"
    cmd = Path(words[0]).name
    if cmd in {"cat", "sed", "head", "tail", "less", "more"}:
        return "read"
    if cmd in {"rg", "grep", "find", "ls"}:
        return "searched"
    if cmd in {"rm", "unlink"}:
        return "deleted"
    if cmd in {"mv"}:
        return "renamed"
    if cmd in {"touch", "mkdir"}:
        return "created"
    if cmd in {"pytest", "py.test"} or "pytest" in words:
        return "tested"
    if cmd in {"ruff", "mypy", "tox", "coverage"}:
        return "tested"
    if cmd == "git" and len(words) > 1 and words[1] in {"add", "commit", "diff", "show"}:
        return "committed" if words[1] == "commit" else "mentioned"
    if _SHELL_REDIRECT_RE.search(command):
        return "edited"
    return "mentioned" if path else "unknown"


def infer_operation(source_kind: str, text: str, meta: dict | None = None) -> str:
    meta = meta or {}
    name = str(meta.get("name") or "").lower()
    lowered = text.lower()
    if source_kind == "diff":
        return "edited"
    if source_kind == "stack_trace" or "traceback" in lowered or "no such file" in lowered:
        return "errored"
    if source_kind == "tool_use" and name in {"edit", "write", "write_file", "apply_patch"}:
        return "edited"
    if source_kind == "tool_use" and name in {"read", "view"}:
        return "read"
    if source_kind == "tool_use" and name in {"grep", "search"}:
        return "searched"
    if source_kind == "command":
        return _command_operation(text, "")
    return "mentioned"


def _source_kind(turn: Turn, text: str) -> str:
    if _STACK_RE.search(text) or "traceback" in text.lower():
        return "stack_trace"
    if _DIFF_RE.search(text):
        return "diff"
    if turn.role == "tool_use":
        name = str(turn.meta.get("name") or "").lower()
        if name in {"shell", "bash"} or "cmd" in text or "command" in text:
            return "command"
        return "tool_use"
    if turn.role == "tool_result":
        return "tool_result"
    return "prose"


def _command_text(text: str) -> str:
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    if isinstance(payload, dict):
        for key in ("cmd", "command", "script"):
            if isinstance(payload.get(key), str):
                return payload[key]
    return text


def extract_file_ref_events(
    session_id: str,
    provider: Tool,
    cwd: str | None,
    turns: list[Turn],
    home: str | None = None,
) -> list[FileRefEvent]:
    events: list[FileRefEvent] = []
    seen: set[tuple[int, str, str]] = set()
    event_index = 0
    for turn_index, turn in enumerate(turns):
        text = turn.text or ""
        command = _command_text(text) if turn.role == "tool_use" else None
        scan_text = command or text
        source_kind = _source_kind(turn, scan_text)
        operation = infer_operation(source_kind, scan_text, turn.meta)
        for raw, line_start, line_end in _path_candidates(scan_text):
            normalized = normalize_path(raw, cwd, home)
            if normalized is None:
                continue
            key = (turn_index, normalized.normalized, operation)
            if key in seen:
                continue
            seen.add(key)
            source_operation = operation
            if source_kind == "stack_trace":
                source_operation = "errored"
            elif source_kind == "diff":
                source_operation = "edited"
            elif source_kind == "command":
                source_operation = _command_operation(scan_text, normalized.normalized)
            confidence = {
                "edited": 0.9,
                "created": 0.9,
                "deleted": 0.9,
                "renamed": 0.9,
                "tested": 0.85,
                "errored": 0.8,
                "read": 0.75,
                "searched": 0.7,
            }.get(source_operation, 0.45)
            excerpt = scan_text[:500]
            events.append(
                FileRefEvent(
                    session_id=session_id,
                    provider=provider,
                    cwd=cwd,
                    turn_index=turn_index,
                    event_index=event_index,
                    source_kind=source_kind,
                    path_raw=raw,
                    path_normalized=normalized.normalized,
                    path_display=normalized.display,
                    path_scope=normalized.scope,
                    basename=normalized.basename,
                    extension=normalized.extension,
                    operation=source_operation,
                    confidence=confidence,
                    line_start=line_start,
                    line_end=line_end,
                    command=command,
                    excerpt=excerpt,
                    exists_now=normalized.exists_now,
                )
            )
            event_index += 1
    return events


def _operation_summary(counter: Counter[str]) -> str:
    order = ["edited", "created", "deleted", "renamed", "tested", "errored", "read", "searched", "committed", "mentioned"]
    parts = []
    for op in order:
        count = counter.get(op, 0)
        if count:
            parts.append(f"{op} {count}x")
    return ", ".join(parts) if parts else "mentioned"


_OPERATION_ORDER = ["edited", "created", "deleted", "renamed", "tested", "errored", "read", "searched", "committed", "mentioned"]


def aggregate_file_refs(events: list[FileRefEvent]) -> list[SessionFileRef]:
    grouped: dict[str, list[FileRefEvent]] = {}
    for event in events:
        grouped.setdefault(event.path_normalized, []).append(event)
    refs: list[SessionFileRef] = []
    for path, group in grouped.items():
        counter = Counter(e.operation for e in group)
        first = min((e.turn_index for e in group if e.turn_index is not None), default=None)
        last = max((e.turn_index for e in group if e.turn_index is not None), default=None)
        exemplar = max(group, key=lambda e: e.confidence)
        operations = [op for op in _OPERATION_ORDER if counter.get(op, 0)]
        exists_values = [e.exists_now for e in group if e.exists_now is not None]
        refs.append(
            SessionFileRef(
                session_id=exemplar.session_id,
                provider=exemplar.provider,
                cwd=exemplar.cwd,
                path_normalized=path,
                path_display=exemplar.path_display,
                path_scope=exemplar.path_scope,
                basename=exemplar.basename,
                extension=exemplar.extension,
                operations=operations,
                operation_summary=_operation_summary(counter),
                mention_count=len(group),
                first_turn_index=first,
                last_turn_index=last,
                max_confidence=max(e.confidence for e in group),
                exists_now=exists_values[-1] if exists_values else None,
            )
        )
    refs.sort(key=lambda r: (-r.max_confidence, r.path_display))
    return refs


def replace_session_file_refs(
    conn: sqlite3.Connection,
    session_id: str,
    events: list[FileRefEvent],
) -> None:
    conn.execute("DELETE FROM session_file_ref_events WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM session_file_refs WHERE session_id = ?", (session_id,))
    now = utcnow_iso()
    conn.executemany(
        """
        INSERT INTO session_file_ref_events (
            session_id, provider, cwd, turn_index, event_index, source_kind,
            path_raw, path_normalized, path_display, path_scope, basename,
            extension, operation, confidence, line_start, line_end, command, excerpt
        ) VALUES (
            :session_id, :provider, :cwd, :turn_index, :event_index, :source_kind,
            :path_raw, :path_normalized, :path_display, :path_scope, :basename,
            :extension, :operation, :confidence, :line_start, :line_end, :command, :excerpt
        )
        """,
        [event.__dict__ for event in events],
    )
    refs = aggregate_file_refs(events)
    conn.executemany(
        """
        INSERT INTO session_file_refs (
            session_id, provider, cwd, path_normalized, path_display,
            path_scope, basename, extension, operations_json,
            operation_summary, mention_count, first_turn_index,
            last_turn_index, max_confidence, exists_now, created_at, updated_at
        ) VALUES (
            :session_id, :provider, :cwd, :path_normalized, :path_display,
            :path_scope, :basename, :extension, :operations_json,
            :operation_summary, :mention_count, :first_turn_index,
            :last_turn_index, :max_confidence, :exists_now, :created_at, :updated_at
        )
        """,
        [
            {
                **ref.__dict__,
                "operations_json": json.dumps(ref.operations),
                "created_at": now,
                "updated_at": now,
            }
            for ref in refs
        ],
    )


def file_ref_icon(operations: list[str]) -> str:
    if "errored" in operations:
        return "!"
    if "edited" in operations:
        return "E"
    if "created" in operations:
        return "C"
    if "tested" in operations:
        return "T"
    if "read" in operations:
        return "R"
    if "searched" in operations:
        return "S"
    return "M"
