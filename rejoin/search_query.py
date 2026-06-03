from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedSearchQuery:
    q: str | None = None
    file: str | None = None
    basename: str | None = None
    ext: str | None = None
    operation: str | None = None
    active: bool | None = None
    pinned: bool | None = None
    tool: str | None = None
    cwd: str | None = None


_INLINE_FILTER_RE = re.compile(
    r'(?P<key>file|basename|ext|op|active|pinned|tool|cwd):(?P<value>"[^"]+"|\'[^\']+\'|\S+)',
    re.IGNORECASE,
)
_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _unquote_filter_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_bool_filter(value: str) -> bool | None:
    lowered = value.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    return None


def parse_search_query(q: str | None) -> ParsedSearchQuery:
    if not q:
        return ParsedSearchQuery()

    filters: dict[str, str] = {}
    parts: list[str] = []
    last = 0
    for match in _INLINE_FILTER_RE.finditer(q):
        key = match.group("key").lower()
        if key == "op":
            key = "operation"
        value = _unquote_filter_value(match.group("value"))
        if key in {"active", "pinned"}:
            bool_value = _parse_bool_filter(value)
            if bool_value is None:
                continue
            parts.append(q[last:match.start()])
            filters[key] = bool_value
        else:
            parts.append(q[last:match.start()])
            filters[key] = value
        last = match.end()
    parts.append(q[last:])
    text = " ".join("".join(parts).split()) or None
    return ParsedSearchQuery(q=text, **filters)


def file_filter_targets(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    if "/" in value or "\\" in value:
        return value.replace("\\", "/"), None
    return None, value
