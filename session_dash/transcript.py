from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class Turn:
    role: str          # 'user', 'assistant', 'tool_use', 'tool_result', 'system'
    text: str
    meta: dict


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"):
                out.append(p.get("text", ""))
        return "\n".join(out)
    return ""


def iter_claude_turns(path: Path) -> Iterator[Turn]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = evt.get("type")
            if et == "user":
                msg = evt.get("message", {}) or {}
                content = msg.get("content", "")
                if isinstance(content, str):
                    yield Turn("user", content, {"ts": evt.get("timestamp")})
                elif isinstance(content, list):
                    for p in content:
                        if isinstance(p, dict):
                            if p.get("type") == "text":
                                yield Turn("user", p.get("text", ""), {"ts": evt.get("timestamp")})
                            elif p.get("type") == "tool_result":
                                result = p.get("content", "")
                                yield Turn("tool_result", _text_of(result) or json.dumps(result)[:2000],
                                           {"tool_use_id": p.get("tool_use_id")})
            elif et == "assistant":
                msg = evt.get("message", {}) or {}
                for p in msg.get("content", []) or []:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        yield Turn("assistant", p.get("text", ""),
                                   {"ts": evt.get("timestamp"), "model": msg.get("model")})
                    elif p.get("type") == "tool_use":
                        yield Turn("tool_use",
                                   json.dumps(p.get("input", {}), indent=2)[:4000],
                                   {"name": p.get("name"), "id": p.get("id")})


def iter_codex_turns(path: Path) -> Iterator[Turn]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") != "response_item":
                continue
            item = evt.get("payload", {}) or {}
            it = item.get("type")
            if it == "message":
                role = item.get("role")
                text = _text_of(item.get("content", []))
                if role in ("user", "assistant") and text:
                    yield Turn(role, text, {"ts": evt.get("timestamp")})
            elif it in ("function_call", "tool_use"):
                args = item.get("arguments") or item.get("input") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                yield Turn("tool_use", json.dumps(args, indent=2)[:4000] if not isinstance(args, str) else args[:4000],
                           {"name": item.get("name") or item.get("tool_name")})
            elif it == "local_shell_call":
                action = item.get("action", {})
                yield Turn("tool_use", json.dumps(action, indent=2)[:4000], {"name": "shell"})
            elif it == "function_call_output":
                output = item.get("output", "")
                if isinstance(output, dict):
                    output = output.get("content", "") or json.dumps(output)
                yield Turn("tool_result", str(output)[:4000], {})


def load_turns(tool: str, path: Path) -> list[Turn]:
    if tool == "claude":
        return list(iter_claude_turns(path))
    if tool == "codex":
        return list(iter_codex_turns(path))
    return []
