from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .brief import (
    brief_markdown,
    build_session_brief,
    source_mtime,
    source_path_exists,
    turn_to_dict,
)
from .common import Tool, iso_to_epoch, short_cwd, utcnow_iso
from .config import (
    ACTIVE_WINDOW_SEC,
    LONG_TURN_CHARS,
    LONG_TURN_LINES,
    REFRESH_INTERVAL_SEC,
    TRANSCRIPT_TAIL,
    TURN_CACHE_SIZE,
)
from .db import connect, init_db, transaction
from .file_refs import extract_file_ref_events, file_ref_icon, replace_session_file_refs
from .indexer import reindex
from .resume import MissingBinary, codexia_url, launch_tmux, resume_command
from .search_query import file_filter_targets, parse_search_query
from .titler import backfill_titles
from .transcript import load_turns

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
log = logging.getLogger("rejoin")

_LAST_INDEXED_AT: float | None = None


class SearchQuerySyntaxError(Exception):
    """User-supplied FTS5 query had invalid syntax."""


def _mark_indexed() -> None:
    global _LAST_INDEXED_AT
    _LAST_INDEXED_AT = datetime.now(UTC).timestamp()


def _changed_count(stats: dict) -> int:
    return sum(
        value
        for key, value in stats.items()
        if isinstance(value, int) and (key.endswith("_new") or key.endswith("_updated"))
    )


def _is_active(last_activity: str | None, now_epoch: float,
               running: set[str] | None = None,
               session_id: str | None = None) -> bool:
    if running and session_id in running:
        return True
    ts = iso_to_epoch(last_activity)
    return bool(ts) and (now_epoch - ts) < ACTIVE_WINDOW_SEC


_RUNNING_CACHE_TTL = 5.0
_running_cache: tuple[float, set[str]] = (0.0, set())


def _running_ids() -> set[str]:
    """`ps aux` scan is ~10-50ms; cache for a few seconds to keep list
    fetches and detail clicks snappy."""
    global _running_cache
    now = datetime.now(UTC).timestamp()
    if now - _running_cache[0] < _RUNNING_CACHE_TTL:
        return _running_cache[1]
    try:
        from .external import running_session_ids
        ids = running_session_ids()
    except Exception:
        ids = set()
    _running_cache = (now, ids)
    return ids


def _highlight(text: str | None, q: str | None) -> Markup:
    if not text:
        return Markup("")
    if not q:
        return Markup(escape(text))
    terms = [t for t in re.split(r"\s+", q.strip()) if len(t) >= 2]
    if not terms:
        return Markup(escape(text))
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    out: list[str] = []
    idx = 0
    for m in pattern.finditer(text):
        out.append(str(escape(text[idx:m.start()])))
        out.append(f"<mark>{escape(m.group(0))}</mark>")
        idx = m.end()
    out.append(str(escape(text[idx:])))
    return Markup("".join(out))


TEMPLATES.env.filters["highlight"] = _highlight
TEMPLATES.env.filters["short_cwd"] = short_cwd


@lru_cache(maxsize=TURN_CACHE_SIZE)
def _load_turns_cached(tool: Tool, path_str: str, mtime: float):
    # mtime is part of the key so the cache invalidates when the file grows.
    return load_turns(tool, Path(path_str))


async def _refresh_loop() -> None:
    while True:
        try:
            stats = await asyncio.to_thread(reindex, False)
            _mark_indexed()
            changed = _changed_count(stats)
            if changed:
                log.info("refresh: %s", stats)
                await backfill_titles()
        except Exception as e:
            log.warning("refresh failed: %s", e)
        await asyncio.sleep(REFRESH_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(_refresh_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="rejoin", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")


def _fetch_sessions(
    tool: str | None,
    cwd: str | None,
    q: str | None,
    file: str | None = None,
    basename: str | None = None,
    ext: str | None = None,
    operation: str | None = None,
    active: bool | None = None,
    pinned: bool | None = None,
    limit: int = 200,
) -> list[dict]:
    parsed = parse_search_query(q)
    q = parsed.q
    parsed_file, parsed_basename = file_filter_targets(parsed.file)
    query_file, query_basename = file_filter_targets(file)
    file = query_file or parsed_file
    basename = basename or query_basename or parsed.basename or parsed_basename
    ext = ext or parsed.ext
    operation = operation or parsed.operation
    active = active if active is not None else parsed.active
    pinned = pinned if pinned is not None else parsed.pinned
    tool = tool or parsed.tool
    cwd = cwd or parsed.cwd

    where: list[str] = []
    params: dict = {"limit": limit}
    if tool:
        where.append("s.tool = :tool")
        params["tool"] = tool
    if cwd:
        where.append("s.cwd = :cwd")
        params["cwd"] = cwd
    if file:
        where.append("(fr.path_normalized = :file OR fr.path_display = :file)")
        params["file"] = file
    if basename:
        where.append("fr.basename = :basename")
        params["basename"] = basename
    if ext:
        ext = ext if ext.startswith(".") else f".{ext}"
        where.append("fr.extension = :ext")
        params["ext"] = ext
    if operation:
        where.append("fr.operations_json LIKE :operation")
        params["operation"] = f'%"{operation}"%'
    if pinned is True:
        where.append("p.pinned_at IS NOT NULL")
    elif pinned is False:
        where.append("p.pinned_at IS NULL")

    sql = """
        SELECT s.*, t.title as ai_title,
               p.pinned_at IS NOT NULL as pinned,
               p.pinned_at as pinned_at,
               COALESCE(fc.file_count, 0) as file_count
        FROM sessions s
        LEFT JOIN titles t ON t.session_id = s.id
        LEFT JOIN pins p ON p.session_id = s.id
        LEFT JOIN (
            SELECT session_id, COUNT(*) as file_count
            FROM session_file_refs
            GROUP BY session_id
        ) fc ON fc.session_id = s.id
    """
    if file or basename or ext or operation:
        sql += " JOIN session_file_refs fr ON fr.session_id = s.id"
    if q:
        sql += " JOIN session_fts f ON f.session_id = s.id WHERE session_fts MATCH :q"
        params["q"] = q
        if where:
            sql += " AND " + " AND ".join(where)
    elif where:
        sql += " WHERE " + " AND ".join(where)
    sql += """
        ORDER BY (p.pinned_at IS NOT NULL) DESC,
                 p.pinned_at DESC,
                 s.last_activity DESC
        LIMIT :limit
    """

    now_epoch = datetime.now(UTC).timestamp()
    running = _running_ids()
    with connect() as conn:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            if q:
                raise SearchQuerySyntaxError from None
            raise
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["active"] = _is_active(d.get("last_activity"), now_epoch,
                                 running, d.get("id"))
        if active is None or d["active"] is active:
            out.append(d)
    return out


def _session_json(row: dict) -> dict:
    return {
        "warnings": _session_warnings(row),
        "id": row["id"],
        "tool": row["tool"],
        "path": row["path"],
        "cwd": row.get("cwd"),
        "started_at": row.get("started_at"),
        "last_activity": row.get("last_activity"),
        "mtime": row.get("mtime"),
        "size": row.get("size"),
        "message_count": row.get("message_count"),
        "tool_call_count": row.get("tool_call_count"),
        "model": row.get("model"),
        "first_prompt": row.get("first_prompt"),
        "last_prompt": row.get("last_prompt"),
        "codex_summary": row.get("codex_summary"),
        "indexed_at": row.get("indexed_at"),
        "title": row.get("ai_title"),
        "pinned": bool(row.get("pinned")),
        "pinned_at": row.get("pinned_at"),
        "active": bool(row.get("active")),
        "file_count": row.get("file_count", 0),
    }


def _session_warnings(row: dict) -> list[str]:
    warnings = []
    src_mtime = source_mtime(row.get("path"))
    if source_path_exists(row.get("path")) is False:
        warnings.append("source_path_missing")
    if src_mtime is None:
        warnings.append("freshness_unknown")
    elif row.get("mtime") is not None and abs(float(src_mtime) - float(row.get("mtime") or 0.0)) >= 1e-6:
        warnings.append("indexed_stale")
    return warnings


def _load_session_tail(row: dict, *, fresh: bool, tail: int) -> dict:
    warnings = _session_warnings(row)
    tail_error = None
    tail_source = "live" if fresh else "indexed"
    try:
        if fresh:
            turns = load_turns(row["tool"], Path(row["path"]))
        else:
            turns = _load_turns_cached(row["tool"], row["path"], row.get("mtime") or 0.0)
    except Exception as e:
        tail_error = str(e)
        tail_source = "unavailable"
        warnings.append("tail_read_failed")
        turns = []
    tail_count = max(0, tail)
    start = max(0, len(turns) - tail_count) if tail_count else len(turns)
    tail_turns = [turn_to_dict(turn, idx) for idx, turn in enumerate(turns[start:], start=start)]
    return {
        "ok": True,
        "warnings": warnings,
        "session_id": row["id"],
        "tail_source": tail_source,
        "tail_error": tail_error,
        "turn_count": len(turns),
        "tail": tail_turns,
    }


def _distinct_cwds() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL ORDER BY cwd"
        )]


def _get_session(session_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, t.title as ai_title,
                      p.pinned_at IS NOT NULL as pinned,
                      p.pinned_at as pinned_at,
                      COALESCE(fc.file_count, 0) as file_count
               FROM sessions s
               LEFT JOIN titles t ON t.session_id = s.id
               LEFT JOIN pins p ON p.session_id = s.id
               LEFT JOIN (
                   SELECT session_id, COUNT(*) as file_count
                   FROM session_file_refs
                   GROUP BY session_id
               ) fc ON fc.session_id = s.id
               WHERE s.id = :id""",
            {"id": session_id},
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["active"] = _is_active(d.get("last_activity"),
                             datetime.now(UTC).timestamp(),
                             _running_ids(), d.get("id"))
    return d


def _get_session_file_refs(session_id: str, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM session_file_refs
            WHERE session_id = :id
            ORDER BY max_confidence DESC, mention_count DESC, path_display
            LIMIT :limit
            """,
            {"id": session_id, "limit": limit},
        ).fetchall()
    refs: list[dict] = []
    for row in rows:
        d = dict(row)
        try:
            operations = json.loads(d.get("operations_json") or "[]")
        except json.JSONDecodeError:
            operations = []
        d["operations"] = operations
        d["icon"] = file_ref_icon(operations)
        d["icon_class"] = {
            "!": "err",
            "E": "edited",
            "C": "created",
            "T": "tested",
            "R": "read",
            "S": "searched",
            "M": "mentioned",
        }.get(d["icon"], "mentioned")
        refs.append(d)
    return refs

def _group_by_cwd(sessions: list[dict]) -> list[dict]:
    pinned = [s for s in sessions if s.get("pinned")]
    others = [s for s in sessions if not s.get("pinned")]
    others.sort(key=lambda s: (s.get("cwd") or "~", -iso_to_epoch(s.get("last_activity"))))

    groups: list[dict] = []
    if pinned:
        groups.append({"cwd": "★ pinned", "sessions": pinned, "pinned_group": True})

    current: dict | None = None
    for s in others:
        c = s.get("cwd") or "(no cwd)"
        if current is None or current["cwd"] != c:
            current = {"cwd": c, "sessions": []}
            groups.append(current)
        current["sessions"].append(s)
    return groups


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    sessions = _fetch_sessions(None, None, None)
    return TEMPLATES.TemplateResponse(
        request, "index.html",
        {"sessions": sessions, "groups": [], "cwds": _distinct_cwds(),
         "q": None, "group": False},
    )


@app.get("/sessions", response_class=HTMLResponse, response_model=None)
def sessions_fragment(
    request: Request,
    tool: str | None = Query(None),
    cwd: str | None = Query(None),
    q: str | None = Query(None),
    file: str | None = Query(None),
    basename: str | None = Query(None),
    ext: str | None = Query(None),
    operation: str | None = Query(None),
    active: bool | None = Query(None),
    pinned: bool | None = Query(None),
    group: bool = Query(False),
) -> HTMLResponse | JSONResponse:
    try:
        sessions = _fetch_sessions(
            tool or None,
            cwd or None,
            q or None,
            file or None,
            basename or None,
            ext or None,
            operation or None,
            active,
            pinned,
        )
    except SearchQuerySyntaxError:
        return JSONResponse({"matches": [], "error": "query syntax"})
    groups = _group_by_cwd(sessions) if group else []
    return TEMPLATES.TemplateResponse(
        request, "_sessions.html",
        {"sessions": sessions, "groups": groups, "q": q, "group": group},
    )


@app.get("/api/sessions")
def api_sessions(
    tool: str | None = Query(None),
    cwd: str | None = Query(None),
    q: str | None = Query(None),
    file: str | None = Query(None),
    basename: str | None = Query(None),
    ext: str | None = Query(None),
    operation: str | None = Query(None),
    active: bool | None = Query(None),
    pinned: bool | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
) -> JSONResponse:
    try:
        sessions = _fetch_sessions(
            tool or None,
            cwd or None,
            q or None,
            file or None,
            basename or None,
            ext or None,
            operation or None,
            active,
            pinned,
            limit,
        )
    except SearchQuerySyntaxError:
        return JSONResponse({"ok": False, "error": "query syntax"}, status_code=400)
    return JSONResponse({"ok": True, "sessions": [_session_json(row) for row in sessions]})


@app.get("/api/sessions/{session_id}")
def api_session(session_id: str) -> JSONResponse:
    row = _get_session(session_id)
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    session = _session_json(row)
    return JSONResponse({"ok": True, "warnings": session["warnings"], "session": session})


@app.get("/api/projects")
def api_projects(limit: int = Query(200, ge=1, le=500)) -> JSONResponse:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.cwd,
                   COUNT(*) as session_count,
                   MAX(s.last_activity) as last_activity,
                   SUM(COALESCE(fc.file_count, 0)) as file_ref_count,
                   GROUP_CONCAT(DISTINCT s.tool) as tools
            FROM sessions s
            LEFT JOIN (
                SELECT session_id, COUNT(*) as file_count
                FROM session_file_refs
                GROUP BY session_id
            ) fc ON fc.session_id = s.id
            GROUP BY s.cwd
            ORDER BY last_activity DESC
            LIMIT :limit
            """,
            {"limit": limit},
        ).fetchall()
    projects = [
        {
            "cwd": row["cwd"],
            "label": short_cwd(row["cwd"]) if row["cwd"] else "(no cwd)",
            "session_count": row["session_count"],
            "file_ref_count": row["file_ref_count"] or 0,
            "last_activity": row["last_activity"],
            "tools": sorted((row["tools"] or "").split(",")) if row["tools"] else [],
        }
        for row in rows
    ]
    return JSONResponse({"ok": True, "projects": projects})


@app.get("/session/{session_id}", response_class=HTMLResponse)
def session_detail(
    request: Request,
    session_id: str,
    full: bool = Query(False),
) -> HTMLResponse:
    row = _get_session(session_id)
    if not row:
        return HTMLResponse("<p>not found</p>", status_code=404)

    all_turns = _load_turns_cached(row["tool"], row["path"], row["mtime"] or 0.0)
    total = len(all_turns)
    if full or total <= TRANSCRIPT_TAIL:
        turns = all_turns
        hidden = 0
    else:
        turns = all_turns[-TRANSCRIPT_TAIL:]
        hidden = total - TRANSCRIPT_TAIL

    blocks: list[dict] = []
    buf: list = []
    for t in turns:
        if t.role in ("tool_use", "tool_result"):
            buf.append(t)
        else:
            if buf:
                blocks.append({"kind": "tools", "turns": buf})
                buf = []
            blocks.append({"kind": "message", "turn": t})
    if buf:
        blocks.append({"kind": "tools", "turns": buf})

    cmd = resume_command(row["tool"], row["id"], row["cwd"])
    cx_url = codexia_url(row["tool"], row["id"], row["cwd"])
    file_refs = _get_session_file_refs(session_id, 20)
    return TEMPLATES.TemplateResponse(
        request, "_detail.html",
        {"s": row, "blocks": blocks, "resume_cmd": cmd,
         "codexia_url": cx_url,
         "hidden": hidden, "total": total,
         "long_lines": LONG_TURN_LINES, "long_chars": LONG_TURN_CHARS,
         "file_refs": file_refs},
    )


@app.post("/session/{session_id}/pin")
def session_pin(session_id: str) -> JSONResponse:
    row = _get_session(session_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    now = utcnow_iso()
    with connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM pins WHERE session_id=:id", {"id": session_id}
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM pins WHERE session_id=:id", {"id": session_id})
            pinned = False
        else:
            conn.execute(
                "INSERT INTO pins (session_id, pinned_at) VALUES (:id, :now)",
                {"id": session_id, "now": now},
            )
            pinned = True
        conn.commit()
    return JSONResponse({"pinned": pinned})


@app.post("/session/{session_id}/resume")
def session_resume(session_id: str) -> JSONResponse:
    row = _get_session(session_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        return JSONResponse(launch_tmux(row["tool"], session_id, row["cwd"]))
    except MissingBinary as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@app.post("/reindex")
async def api_reindex(
    titles: bool = Query(True),
    force: bool = Query(False),
) -> JSONResponse:
    stats = await asyncio.to_thread(reindex, force)
    _mark_indexed()
    if titles:
        stats["titles"] = await backfill_titles()
    return JSONResponse(stats)


@app.get("/api/sessions/{session_id}/files")
def api_session_files(session_id: str) -> JSONResponse:
    if not _get_session(session_id):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    files = [
        {
            "path": ref["path_display"],
            "path_normalized": ref["path_normalized"],
            "path_scope": ref["path_scope"],
            "basename": ref["basename"],
            "extension": ref["extension"],
            "operations": ref["operations"],
            "operation_summary": ref["operation_summary"],
            "mention_count": ref["mention_count"],
            "first_turn_index": ref["first_turn_index"],
            "last_turn_index": ref["last_turn_index"],
            "exists_now": bool(ref["exists_now"]) if ref["exists_now"] is not None else None,
            "confidence": ref["max_confidence"],
        }
        for ref in _get_session_file_refs(session_id, 200)
    ]
    return JSONResponse({"ok": True, "session_id": session_id, "files": files})


@app.get("/api/sessions/{session_id}/tail")
def api_session_tail(
    session_id: str,
    fresh: bool = Query(True),
    tail: int = Query(12, ge=0, le=200),
) -> JSONResponse:
    row = _get_session(session_id)
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse(_load_session_tail(row, fresh=fresh, tail=tail))


@app.get("/api/sessions/{session_id}/brief", response_model=None)
def api_session_brief(
    session_id: str,
    fresh: bool = Query(True),
    tail: int = Query(12, ge=0, le=200),
    brief_format: str = Query("json", alias="format", pattern="^(json|markdown)$"),
) -> JSONResponse | PlainTextResponse:
    row = _get_session(session_id)
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    try:
        brief = build_session_brief(
            row,
            _get_session_file_refs(session_id, 200),
            fresh=fresh,
            tail=tail,
            cached_loader=_load_turns_cached,
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if brief_format == "markdown":
        return PlainTextResponse(brief_markdown(brief), media_type="text/markdown")
    return JSONResponse(brief)


@app.post("/api/sessions/{session_id}/file-refs/rebuild")
def api_session_file_refs_rebuild(session_id: str) -> JSONResponse:
    row = _get_session(session_id)
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    try:
        turns = load_turns(row["tool"], Path(row["path"]))
        events = extract_file_ref_events(row["id"], row["tool"], row.get("cwd"), turns)
        with connect() as conn:
            with transaction(conn):
                replace_session_file_refs(conn, row["id"], events)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({
        "ok": True,
        "session_id": session_id,
        "event_count": len(events),
        "file_ref_count": len(_get_session_file_refs(session_id, 200)),
    })


@app.get("/api/diagnostics")
def api_diagnostics() -> JSONResponse:
    with connect() as conn:
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        file_ref_count = conn.execute("SELECT COUNT(*) FROM session_file_refs").fetchone()[0]
        file_ref_event_count = conn.execute(
            "SELECT COUNT(*) FROM session_file_ref_events"
        ).fetchone()[0]
        provider_counts = {
            row["tool"]: row["count"]
            for row in conn.execute(
                "SELECT tool, COUNT(*) as count FROM sessions GROUP BY tool ORDER BY tool"
            )
        }
        provider_file_ref_counts = {
            row["provider"]: row["count"]
            for row in conn.execute(
                """
                SELECT provider, COUNT(*) as count
                FROM session_file_refs
                GROUP BY provider
                ORDER BY provider
                """
            )
        }
    return JSONResponse({
        "ok": True,
        "schema": {"user_version": schema_version},
        "sessions": {"count": session_count, "by_provider": provider_counts},
        "file_refs": {
            "count": file_ref_count,
            "event_count": file_ref_event_count,
            "by_provider": provider_file_ref_counts,
        },
        "providers": provider_counts,
    })


@app.get("/api/files/search")
def api_files_search(
    q: str | None = Query(None),
    basename: str | None = Query(None),
    ext: str | None = Query(None),
    cwd: str | None = Query(None),
    tool: str | None = Query(None),
    operation: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    where: list[str] = []
    params: dict = {"limit": limit, "offset": offset}
    if q:
        where.append("(fr.path_normalized LIKE :q OR fr.path_display LIKE :q)")
        params["q"] = f"%{q}%"
    if basename:
        where.append("fr.basename = :basename")
        params["basename"] = basename
    if ext:
        ext = ext if ext.startswith(".") else f".{ext}"
        where.append("fr.extension = :ext")
        params["ext"] = ext
    if cwd:
        where.append("fr.cwd = :cwd")
        params["cwd"] = cwd
    if tool:
        where.append("fr.provider = :tool")
        params["tool"] = tool
    if operation:
        where.append("fr.operations_json LIKE :operation")
        params["operation"] = f'%"{operation}"%'
    sql = """
        SELECT fr.*, s.last_activity, s.first_prompt, s.tool, t.title as ai_title
        FROM session_file_refs fr
        JOIN sessions s ON s.id = fr.session_id
        LEFT JOIN titles t ON t.session_id = s.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += """
        ORDER BY s.last_activity DESC, fr.max_confidence DESC
        LIMIT :limit OFFSET :offset
    """
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    files = []
    for row in rows:
        d = dict(row)
        try:
            operations = json.loads(d.get("operations_json") or "[]")
        except json.JSONDecodeError:
            operations = []
        files.append({
            "session_id": d["session_id"],
            "tool": d["tool"],
            "title": d.get("ai_title") or (d.get("first_prompt") or "")[:80],
            "last_activity": d.get("last_activity"),
            "path": d["path_display"],
            "path_normalized": d["path_normalized"],
            "basename": d["basename"],
            "extension": d["extension"],
            "operations": operations,
            "operation_summary": d["operation_summary"],
            "mention_count": d["mention_count"],
            "confidence": d["max_confidence"],
        })
    return JSONResponse({"ok": True, "files": files})


@app.get("/status")
def api_status() -> JSONResponse:
    age = None
    if _LAST_INDEXED_AT is not None:
        age = datetime.now(UTC).timestamp() - _LAST_INDEXED_AT
    return JSONResponse({"last_indexed_age_s": age})


def main() -> None:
    """Entrypoint for `rejoin` console script. Runs uvicorn with config values."""
    import errno
    import os
    import sys

    import uvicorn

    from .config import HOST, PORT

    host = os.environ.get("REJOIN_HOST") or HOST
    port = int(os.environ.get("REJOIN_PORT") or PORT)
    try:
        uvicorn.run("rejoin.app:app", host=host, port=port)
    except OSError as e:
        if e.errno == errno.EADDRINUSE or "Address already in use" in str(e):
            print(f"port {port} in use — set REJOIN_PORT or adjust config", file=sys.stderr)
            raise SystemExit(1) from e
        raise


if __name__ == "__main__":
    main()
