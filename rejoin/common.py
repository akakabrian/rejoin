from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Tool = Literal["claude", "codex", "opencode", "pi", "openclaw"]

TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})

_HOME_STR = str(Path.home())
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def iso_to_epoch(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") in TEXT_PART_TYPES
        )
    return ""


def short_cwd(cwd: str | None) -> str:
    if not cwd:
        return ""
    return cwd.replace(_HOME_STR, "~") if cwd.startswith(_HOME_STR) else cwd


def uuid_from_stem(stem: str) -> str:
    m = _UUID_RE.search(stem)
    return m.group(0) if m else stem


def ago(last_activity: str | None, now_epoch: float | None = None) -> str:
    """Compact relative time: `-` (<1 min), `Nm`, `Nh`, `Nd`, `Ny`.

    Rounds down to the largest unit that fits.
    """
    ts = iso_to_epoch(last_activity)
    if ts == 0.0:
        return ""
    if now_epoch is None:
        now_epoch = datetime.now(UTC).timestamp()
    delta = max(0.0, now_epoch - ts)
    if delta < 60:
        return "-"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    if delta < 86400 * 365:
        return f"{int(delta // 86400)}d"
    return f"{int(delta // (86400 * 365))}y"
