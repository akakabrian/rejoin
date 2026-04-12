from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .db import connect, init_db
from .indexer import reindex
from .resume import launch_tmux, resume_command
from .titler import backfill_titles
from .transcript import load_turns

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
log = logging.getLogger("session_dash")

REFRESH_INTERVAL_SEC = 60
DEFAULT_TRANSCRIPT_TAIL = 40
ACTIVE_WINDOW_SECONDS = 120


def _iso_to_epoch(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _is_active(last_activity: str | None, now_epoch: float) -> bool:
    ts = _iso_to_epoch(last_activity)
    return bool(ts) and (now_epoch - ts) < ACTIVE_WINDOW_SECONDS


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


async def _refresh_loop() -> None:
    while True:
        try:
            stats = await asyncio.to_thread(reindex, False)
            changed = stats["claude_new"] + stats["claude_updated"] + stats["codex_new"] + stats["codex_updated"]
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


app = FastAPI(title="session-dash", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _fetch_sessions(
    tool: str | None,
    cwd: str | None,
    q: str | None,
    limit: int = 200,
) -> list[dict]:
    where = []
    params: dict = {}
    if tool:
        where.append("s.tool = :tool")
        params["tool"] = tool
    if cwd:
        where.append("s.cwd = :cwd")
        params["cwd"] = cwd

    base = """
        SELECT s.*, t.title as ai_title,
               p.pinned_at IS NOT NULL as pinned,
               p.pinned_at as pinned_at
        FROM sessions s
        LEFT JOIN titles t ON t.session_id = s.id
        LEFT JOIN pins p ON p.session_id = s.id
    """
    if q:
        sql = base + """
            JOIN session_fts f ON f.session_id = s.id
            WHERE session_fts MATCH :q
        """
        params["q"] = q
        if where:
            sql += " AND " + " AND ".join(where)
    else:
        sql = base
        if where:
            sql += " WHERE " + " AND ".join(where)
    sql += """
        ORDER BY (p.pinned_at IS NOT NULL) DESC,
                 p.pinned_at DESC,
                 s.last_activity DESC
        LIMIT :limit
    """
    params["limit"] = limit

    now_epoch = datetime.now(timezone.utc).timestamp()
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["active"] = _is_active(d.get("last_activity"), now_epoch)
            out.append(d)
        return out


def _distinct_cwds() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL ORDER BY cwd"
        )]


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    sessions = _fetch_sessions(None, None, None)
    cwds = _distinct_cwds()
    return TEMPLATES.TemplateResponse(
        request, "index.html",
        {"sessions": sessions, "groups": [], "cwds": cwds,
         "filters": {}, "q": None, "group": False},
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

    groups: list[dict] = []
    if group:
        # keep pinned sessions together at the top, then group the rest by cwd
        pinned = [s for s in sessions if s.get("pinned")]
        others = [s for s in sessions if not s.get("pinned")]
        if pinned:
            groups.append({"cwd": "★ pinned", "sessions": pinned, "pinned_group": True})
        others.sort(key=lambda s: (s.get("cwd") or "~", -_iso_to_epoch(s.get("last_activity"))))
        cur_cwd: object = object()
        cur_group: dict | None = None
        for s in others:
            c = s.get("cwd") or "(no cwd)"
            if c != cur_cwd:
                cur_cwd = c
                cur_group = {"cwd": c, "sessions": []}
                groups.append(cur_group)
            cur_group["sessions"].append(s)

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
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, t.title as ai_title, p.pinned_at IS NOT NULL as pinned
               FROM sessions s
               LEFT JOIN titles t ON t.session_id=s.id
               LEFT JOIN pins p ON p.session_id=s.id
               WHERE s.id=:id""",
            {"id": session_id},
        ).fetchone()
    if not row:
        return HTMLResponse("<p>not found</p>", status_code=404)
    row_d = dict(row)
    row_d["active"] = _is_active(row_d.get("last_activity"),
                                 datetime.now(timezone.utc).timestamp())
    all_turns = load_turns(row_d["tool"], Path(row_d["path"]))
    total = len(all_turns)
    if full or total <= DEFAULT_TRANSCRIPT_TAIL:
        turns = all_turns
        hidden = 0
    else:
        turns = all_turns[-DEFAULT_TRANSCRIPT_TAIL:]
        hidden = total - DEFAULT_TRANSCRIPT_TAIL

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

    cmd = resume_command(row_d["tool"], row_d["id"], row_d["cwd"])
    return TEMPLATES.TemplateResponse(
        request, "_detail.html",
        {
            "s": row_d,
            "blocks": blocks,
            "resume_cmd": cmd,
            "hidden": hidden,
            "total": total,
        },
    )


@app.post("/session/{session_id}/pin")
def session_pin(session_id: str) -> JSONResponse:
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM pins WHERE session_id=:id", {"id": session_id}
        ).fetchone()
        if exists:
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
    with connect() as conn:
        row = conn.execute(
            "SELECT tool, cwd FROM sessions WHERE id=:id", {"id": session_id}
        ).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    info = launch_tmux(row["tool"], session_id, row["cwd"])
    return JSONResponse(info)


@app.post("/reindex")
async def api_reindex(titles: bool = Query(False)) -> JSONResponse:
    stats = await asyncio.to_thread(reindex, False)
    if titles:
        title_stats = await backfill_titles()
        stats["titles"] = title_stats
    return JSONResponse(stats)
