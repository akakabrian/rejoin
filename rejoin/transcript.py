from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .common import Tool, iter_jsonl, text_of


@dataclass
class Turn:
    role: str          # 'user' | 'assistant' | 'tool_use' | 'tool_result' | 'system'
    text: str
    meta: dict


def _fmt_args(obj, limit: int = 4000) -> str:
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return obj[:limit]
    return json.dumps(obj, indent=2)[:limit]


def iter_claude_turns(path: Path) -> Iterator[Turn]:
    for evt in iter_jsonl(path):
        et = evt.get("type")
        if et == "user":
            msg = evt.get("message", {}) or {}
            content = msg.get("content", "")
            if isinstance(content, str):
                yield Turn("user", content, {"ts": evt.get("timestamp")})
            elif isinstance(content, list):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        yield Turn("user", p.get("text", ""), {"ts": evt.get("timestamp")})
                    elif p.get("type") == "tool_result":
                        result = p.get("content", "")
                        yield Turn(
                            "tool_result",
                            text_of(result) or _fmt_args(result, 2000),
                            {"tool_use_id": p.get("tool_use_id")},
                        )
        elif et == "assistant":
            msg = evt.get("message", {}) or {}
            for p in msg.get("content", []) or []:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    yield Turn("assistant", p.get("text", ""),
                               {"ts": evt.get("timestamp"), "model": msg.get("model")})
                elif p.get("type") == "tool_use":
                    yield Turn("tool_use", _fmt_args(p.get("input", {})),
                               {"name": p.get("name"), "id": p.get("id")})


def iter_codex_turns(path: Path) -> Iterator[Turn]:
    for evt in iter_jsonl(path):
        if evt.get("type") != "response_item":
            continue
        item = evt.get("payload", {}) or {}
        it = item.get("type")
        if it == "message":
            role = item.get("role")
            text = text_of(item.get("content", []))
            if role in ("user", "assistant") and text:
                yield Turn(role, text, {"ts": evt.get("timestamp")})
        elif it in ("function_call", "tool_use"):
            args = item.get("arguments") or item.get("input") or {}
            yield Turn("tool_use", _fmt_args(args),
                       {"name": item.get("name") or item.get("tool_name")})
        elif it == "local_shell_call":
            yield Turn("tool_use", _fmt_args(item.get("action", {})), {"name": "shell"})
        elif it == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, dict):
                output = output.get("content", "") or json.dumps(output)
            yield Turn("tool_result", str(output)[:4000], {})


def iter_openclaw_turns(path: Path) -> Iterator[Turn]:
    """OpenClaw JSONL: header line + message lines with
    message.role, message.content (str or list of typed blocks)."""
    for evt in iter_jsonl(path):
        if evt.get("type") != "message":
            continue
        msg = evt.get("message", {}) or {}
        role = msg.get("role")
        ts = evt.get("timestamp")
        content = msg.get("content", "")
        if isinstance(content, str):
            if role in ("user", "assistant") and content:
                yield Turn(role, content, {"ts": ts, "model": msg.get("model")})
            continue
        if not isinstance(content, list):
            continue
        for p in content:
            if not isinstance(p, dict):
                continue
            pt = p.get("type")
            if pt == "text":
                if role in ("user", "assistant") and p.get("text"):
                    yield Turn(role, p.get("text", ""),
                               {"ts": ts, "model": msg.get("model")})
            elif pt == "toolCall":
                yield Turn("tool_use", _fmt_args(p.get("input", {})),
                           {"name": p.get("name") or p.get("toolName")})
            elif pt == "toolResult":
                out = p.get("output", "") or p.get("content", "")
                yield Turn("tool_result", str(out)[:4000], {})


_ITERATORS: dict[Tool, Callable[[Path], Iterator[Turn]]] = {
    "claude": iter_claude_turns,
    "codex": iter_codex_turns,
    "openclaw": iter_openclaw_turns,
}


def load_turns(tool: Tool, path: Path) -> list[Turn]:
    if tool in _ITERATORS:
        return list(_ITERATORS[tool](path))
    # OpenCode/Pi come from agent-sessions and use session IDs, not file
    # paths. `path` is "agent-sessions://<tool>/<id>" — recover the id.
    if tool in ("opencode", "pi"):
        session_id = str(path).rsplit("/", 1)[-1]
        try:
            from .external import iter_external_turns
        except Exception:
            return []
        return list(iter_external_turns(tool, session_id))
    raise ValueError(f"unknown tool: {tool}")
