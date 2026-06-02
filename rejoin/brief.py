from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .common import Tool, utcnow_iso
from .transcript import Turn, load_turns

TurnLoader = Callable[[Tool, str, float], list[Turn]]


def source_mtime(path: str | None) -> float | None:
    if not path or "://" in path:
        return None
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def source_path_exists(path: str | None) -> bool | None:
    if not path or "://" in path:
        return None
    return Path(path).exists()


def sentence(text: str | None, limit: int = 220) -> str | None:
    if not text:
        return None
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit - 1].rstrip() + "..."


def turn_to_dict(turn: Turn, index: int) -> dict:
    return {
        "index": index,
        "role": turn.role,
        "text": turn.text,
        "meta": turn.meta,
    }


def indexed_tail(row: dict) -> list[Turn]:
    turns = []
    if row.get("first_prompt"):
        turns.append(Turn("user", row["first_prompt"], {"source": "indexed:first_prompt"}))
    if row.get("codex_summary"):
        turns.append(Turn("assistant", row["codex_summary"], {"source": "indexed:codex_summary"}))
    if row.get("last_prompt") and row.get("last_prompt") != row.get("first_prompt"):
        turns.append(Turn("user", row["last_prompt"], {"source": "indexed:last_prompt"}))
    return turns


def operation_signals(file_refs: list[dict]) -> list[str]:
    signals: list[str] = []
    seen: set[str] = set()
    for ref in file_refs:
        for op in ref.get("operations") or []:
            if op not in seen:
                seen.add(op)
                signals.append(op)
    return signals


def build_summary(row: dict, file_refs: list[dict], tail_turns: list[dict]) -> dict:
    title = row.get("ai_title")
    first_prompt = sentence(row.get("first_prompt"))
    cwd = row.get("cwd")
    codex_summary = sentence(row.get("codex_summary"), 360)

    overall_parts = []
    if title:
        overall_parts.append(f"Title: {title}.")
    if first_prompt:
        overall_parts.append(f"Started with: {first_prompt}")
    if cwd:
        overall_parts.append(f"Workspace: {cwd}.")
    if codex_summary:
        overall_parts.append(f"Indexed summary: {codex_summary}")
    overall = " ".join(overall_parts) or "No indexed session prompt or summary is available."

    progress_parts = []
    if codex_summary:
        progress_parts.append(codex_summary)
    if file_refs:
        ops = operation_signals(file_refs)
        op_text = ", ".join(ops)
        top_files = ", ".join(ref["path_display"] for ref in file_refs[:5])
        progress_parts.append(f"Referenced {len(file_refs)} files: {top_files}.")
        if op_text:
            progress_parts.append(f"File operation signals: {op_text}.")
    notable = [
        sentence(turn["text"], 180)
        for turn in tail_turns
        if turn["role"] in {"assistant", "tool_result"} and turn.get("text")
    ]
    if notable:
        progress_parts.append("Recent notable turn: " + notable[-1])
    progress = " ".join(progress_parts) or "No progress evidence has been extracted yet."

    tail = [
        f"{turn['index']}: {turn['role']}: {sentence(turn['text'], 240) or ''}"
        for turn in tail_turns
    ]
    return {"overall": overall, "progress": progress, "tail": tail}


def files_for_brief(file_refs: list[dict]) -> list[dict]:
    return [
        {
            "path": ref["path_display"],
            "path_normalized": ref["path_normalized"],
            "path_scope": ref["path_scope"],
            "basename": ref["basename"],
            "extension": ref["extension"],
            "operations": ref["operations"],
            "operation_summary": ref["operation_summary"],
            "mention_count": ref["mention_count"],
            "exists_now": bool(ref["exists_now"]) if ref["exists_now"] is not None else None,
            "confidence": ref["max_confidence"],
        }
        for ref in file_refs
    ]


def _live_load_turns(tool: Tool, path_str: str, mtime: float) -> list[Turn]:
    return load_turns(tool, Path(path_str))


def build_session_brief(
    row: dict,
    file_refs: list[dict],
    *,
    fresh: bool,
    tail: int,
    cached_loader: TurnLoader | None = None,
    live_loader: TurnLoader = _live_load_turns,
) -> dict:
    src_mtime = source_mtime(row.get("path"))
    src_exists = source_path_exists(row.get("path"))
    indexed_mtime = row.get("mtime")
    freshness_known = src_mtime is not None and indexed_mtime is not None
    indexed_is_stale = None
    if freshness_known:
        indexed_is_stale = abs(float(src_mtime) - float(indexed_mtime or 0.0)) >= 1e-6

    tail_error = None
    tail_source = "live" if fresh else "indexed"
    tail_is_live = bool(fresh)
    try:
        if fresh:
            turns = live_loader(row["tool"], row["path"], row.get("mtime") or 0.0)
        elif cached_loader is not None:
            turns = cached_loader(row["tool"], row["path"], row.get("mtime") or 0.0)
        else:
            turns = indexed_tail(row)
    except Exception as e:
        tail_error = str(e)
        tail_source = "indexed_fallback"
        tail_is_live = False
        turns = indexed_tail(row)

    tail_count = max(0, tail)
    start = max(0, len(turns) - tail_count) if tail_count else len(turns)
    tail_turns = [turn_to_dict(turn, idx) for idx, turn in enumerate(turns[start:], start=start)]
    summary = build_summary(row, file_refs, tail_turns)
    last_turn = tail_turns[-1] if tail_turns else None
    all_turns = [turn_to_dict(turn, idx) for idx, turn in enumerate(turns)]
    latest_user_turn = next((turn for turn in reversed(all_turns) if turn["role"] == "user"), None)
    latest_assistant_turn = next(
        (turn for turn in reversed(all_turns) if turn["role"] == "assistant"), None
    )
    file_ref_count = len(file_refs)
    files_source = "session_file_refs"
    turn_count_source = tail_source
    summary_source = "indexed_extract"
    if tail_turns and tail_source == "live":
        summary_source = "indexed_extract+live_tail"
    elif tail_turns and tail_source == "indexed_fallback":
        summary_source = "indexed_extract+fallback_tail"
    warnings = []
    if not freshness_known:
        warnings.append("freshness_unknown")
    elif indexed_is_stale:
        warnings.append("indexed_stale")
    if tail_error:
        warnings.append("tail_read_failed")
    if file_ref_count and indexed_is_stale:
        warnings.append("file_refs_may_be_stale")

    return {
        "ok": True,
        "generated_at": utcnow_iso(),
        "warnings": warnings,
        "session_id": row["id"],
        "tool": row["tool"],
        "title": row.get("ai_title"),
        "cwd": row.get("cwd"),
        "summary": summary,
        "freshness": {
            "active": bool(row.get("active")),
            "source_mtime": src_mtime,
            "source_path_exists": src_exists,
            "indexed_mtime": indexed_mtime,
            "indexed_is_stale": indexed_is_stale,
            "tail_is_live": tail_is_live,
            "tail_source": tail_source,
            "summary_source": summary_source,
            "files_source": files_source,
            "file_ref_count": file_ref_count,
            "files_may_be_stale": indexed_is_stale if file_ref_count else False,
            "freshness_known": freshness_known,
            "turn_count": len(turns),
            "turn_count_source": turn_count_source,
            "last_turn_index": last_turn["index"] if last_turn else None,
            "last_turn_role": last_turn["role"] if last_turn else None,
            "last_turn_at": (last_turn.get("meta") or {}).get("ts") if last_turn else None,
            "last_indexed_at": row.get("indexed_at"),
        },
        "tail_error": tail_error,
        "files": files_for_brief(file_refs),
        "tail": tail_turns,
        "latest_user_turn": latest_user_turn,
        "latest_assistant_turn": latest_assistant_turn,
    }


def brief_markdown(brief: dict) -> str:
    freshness = brief["freshness"]
    if freshness["freshness_known"]:
        if freshness["indexed_is_stale"]:
            freshness_note = (
                "Freshness: live transcript metadata differs from the indexed session; "
                "tail data may be live, but indexed file refs may lag."
            )
        else:
            freshness_note = "Freshness: indexed metadata matches the backing transcript mtime."
    else:
        freshness_note = (
            "Freshness: backing transcript mtime is unavailable, so stale status is unknown."
        )
    lines = [
        f"# Session Brief: {brief['session_id']}",
        "",
        freshness_note,
        "",
        "## Overall",
        brief["summary"]["overall"],
        "",
        "## Progress",
        brief["summary"]["progress"],
        "",
        "## Freshness",
        f"- active: {str(freshness['active']).lower()}",
        f"- source_mtime: {freshness['source_mtime']}",
        f"- source_path_exists: {freshness['source_path_exists']}",
        f"- indexed_mtime: {freshness['indexed_mtime']}",
        f"- indexed_is_stale: {freshness['indexed_is_stale']}",
        f"- tail_is_live: {str(freshness['tail_is_live']).lower()}",
        f"- tail_source: {freshness['tail_source']}",
        f"- summary_source: {freshness['summary_source']}",
        f"- files_source: {freshness['files_source']}",
        f"- file_ref_count: {freshness['file_ref_count']}",
        f"- files_may_be_stale: {freshness['files_may_be_stale']}",
        f"- freshness_known: {str(freshness['freshness_known']).lower()}",
        f"- turn_count: {freshness['turn_count']}",
        f"- turn_count_source: {freshness['turn_count_source']}",
        f"- last_turn_index: {freshness['last_turn_index']}",
        f"- last_turn_role: {freshness['last_turn_role']}",
        f"- last_turn_at: {freshness['last_turn_at']}",
        f"- last_indexed_at: {freshness['last_indexed_at']}",
        "",
        "## Files",
    ]
    if brief["files"]:
        for file in brief["files"]:
            ops = ", ".join(file["operations"]) or "mentioned"
            lines.append(f"- {file['path']} ({ops}; {file['mention_count']} mentions)")
    else:
        lines.append("- none")
    lines.extend(["", "## Tail"])
    if brief.get("tail_error"):
        lines.append(f"_Tail live-read error: {brief['tail_error']}_")
        lines.append("")
    if brief["tail"]:
        for turn in brief["tail"]:
            lines.append(f"### {turn['index']} · {turn['role']}")
            ts = (turn.get("meta") or {}).get("ts")
            if ts:
                lines.append(f"_at {ts}_")
            lines.append("")
            lines.append("```text")
            lines.append(sentence(turn["text"], 1200) or "")
            lines.append("```")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"
