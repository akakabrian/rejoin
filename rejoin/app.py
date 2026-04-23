from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .common import Tool, iso_to_epoch, short_cwd, utcnow_iso
from .config import (
    ACTIVE_WINDOW_SEC,
    LONG_TURN_CHARS,
    LONG_TURN_LINES,
    REFRESH_INTERVAL_SEC,
    TRANSCRIPT_TAIL,
    TURN_CACHE_SIZE,
)
from .db import connect, init_db
from .indexer import reindex
from .resume import MissingBinary, launch_tmux, resume_command
from .titler import backfill_titles
from .transcript import load_turns

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
log = logging.getLogger("rejoin")

_LAST_INDEXED_AT: float | None = None


def _mark_indexed() -> None:
    global _LAST_INDEXED_AT
    _LAST_INDEXED_AT = datetime.now(UTC).timestamp()


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
            changed = (stats["claude_new"] + stats["claude_updated"]
                       + stats["codex_new"] + stats["codex_updated"])
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
    limit: int = 200,
) -> list[dict]:
    where: list[str] = []
    params: dict = {"limit": limit}
    if tool:
        where.append("s.tool = :tool")
        params["tool"] = tool
    if cwd:
        where.append("s.cwd = :cwd")
        params["cwd"] = cwd

    sql = """
        SELECT s.*, t.title as ai_title,
               p.pinned_at IS NOT NULL as pinned,
               p.pinned_at as pinned_at
        FROM sessions s
        LEFT JOIN titles t ON t.session_id = s.id
        LEFT JOIN pins p ON p.session_id = s.id
    """
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
        rows = conn.execute(sql, params).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["active"] = _is_active(d.get("last_activity"), now_epoch,
                                 running, d.get("id"))
        out.append(d)
    return out


def _distinct_cwds() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL ORDER BY cwd"
        )]


def _get_session(session_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, t.title as ai_title, p.pinned_at IS NOT NULL as pinned
               FROM sessions s
               LEFT JOIN titles t ON t.session_id = s.id
               LEFT JOIN pins p ON p.session_id = s.id
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


@app.get("/sessions", response_class=HTMLResponse)
def sessions_fragment(
    request: Request,
    tool: str | None = Query(None),
    cwd: str | None = Query(None),
    q: str | None = Query(None),
    group: bool = Query(False),
) -> HTMLResponse:
    sessions = _fetch_sessions(tool or None, cwd or None, q or None)
    groups = _group_by_cwd(sessions) if group else []
    return TEMPLATES.TemplateResponse(
        request, "_sessions.html",
        {"sessions": sessions, "groups": groups, "q": q, "group": group},
    )


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
    return TEMPLATES.TemplateResponse(
        request, "_detail.html",
        {"s": row, "blocks": blocks, "resume_cmd": cmd,
         "hidden": hidden, "total": total,
         "long_lines": LONG_TURN_LINES, "long_chars": LONG_TURN_CHARS},
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
async def api_reindex(titles: bool = Query(True)) -> JSONResponse:
    stats = await asyncio.to_thread(reindex, False)
    _mark_indexed()
    if titles:
        stats["titles"] = await backfill_titles()
    return JSONResponse(stats)


@app.get("/status")
def api_status() -> JSONResponse:
    age = None
    if _LAST_INDEXED_AT is not None:
        age = datetime.now(UTC).timestamp() - _LAST_INDEXED_AT
    return JSONResponse({"last_indexed_age_s": age})


def main() -> None:
    """Entrypoint for `rejoin` console script. Runs uvicorn with config values."""
    import os

    import uvicorn

    from .config import HOST, PORT

    host = os.environ.get("REJOIN_HOST") or HOST
    port = int(os.environ.get("REJOIN_PORT") or PORT)
    uvicorn.run("rejoin.app:app", host=host, port=port)
