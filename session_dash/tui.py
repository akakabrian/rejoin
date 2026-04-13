"""Textual-based TUI for session-dash.

Shares the same SQLite index with the web app; both can run simultaneously.
Inside tmux, rejoin opens a new window in the current session and switches
to it. Outside tmux, it starts a detached session and prints the attach
command.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .common import Tool, iso_to_epoch, short_cwd
from .config import ACTIVE_WINDOW_SEC, TRANSCRIPT_TAIL, TURN_CACHE_SIZE
from .db import connect, init_db
from .indexer import reindex
from .resume import resume_command, tmux_session_name
from .transcript import load_turns


# ---------- data access ----------


def _fetch_sessions(q: str | None = None, limit: int = 500) -> list[dict]:
    sql = """
        SELECT s.*, t.title as ai_title,
               p.pinned_at IS NOT NULL as pinned
        FROM sessions s
        LEFT JOIN titles t ON t.session_id = s.id
        LEFT JOIN pins p ON p.session_id = s.id
    """
    params: dict = {"limit": limit}
    if q:
        sql += " JOIN session_fts f ON f.session_id = s.id WHERE session_fts MATCH :q"
        params["q"] = q
    sql += """
        ORDER BY (p.pinned_at IS NOT NULL) DESC,
                 p.pinned_at DESC,
                 s.last_activity DESC
        LIMIT :limit
    """
    now = datetime.now(timezone.utc).timestamp()
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        ts = iso_to_epoch(r.get("last_activity"))
        r["active"] = bool(ts) and (now - ts) < ACTIVE_WINDOW_SEC
    return rows


def _toggle_pin(session_id: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
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
    return pinned


@lru_cache(maxsize=TURN_CACHE_SIZE)
def _cached_turns(tool: Tool, path_str: str, mtime: float):
    return load_turns(tool, Path(path_str))


# ---------- tmux-aware resume ----------


def _rejoin(tool: Tool, session_id: str, cwd: str | None) -> str:
    """Launch/attach/select the tmux session and return a status line."""
    cmd = resume_command(tool, session_id, cwd)
    name = tmux_session_name(tool, session_id)

    if os.environ.get("TMUX"):
        # we're inside tmux — open a new window in the current server
        existing = subprocess.run(
            ["tmux", "list-windows", "-F", "#{window_name}"],
            capture_output=True, text=True,
        )
        if name not in existing.stdout.splitlines():
            subprocess.run(
                ["tmux", "new-window", "-n", name, "bash", "-lc", cmd],
                check=True,
            )
        else:
            subprocess.run(["tmux", "select-window", "-t", name], check=True)
        return f"opened tmux window [b]{name}[/b]"

    # outside tmux — start a detached session
    has = subprocess.run(
        ["tmux", "has-session", "-t", name], capture_output=True
    )
    if has.returncode != 0:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "bash", "-lc", cmd],
            check=True,
        )
    return f"started tmux session [b]{name}[/b] — attach: [b]tmux attach -t {shlex.quote(name)}[/b]"


# ---------- transcript rendering ----------


def _render_transcript(tool: Tool, path: str, mtime: float) -> list[Text]:
    turns = _cached_turns(tool, path, mtime)
    total = len(turns)
    tail = turns[-TRANSCRIPT_TAIL:] if total > TRANSCRIPT_TAIL else turns
    hidden = total - len(tail)

    out: list[Text] = []
    if hidden:
        out.append(Text(f"— {hidden} earlier turns hidden ({total} total) —",
                        style="#968a77 italic"))
        out.append(Text(""))

    buf: list = []
    last_role: str | None = None

    def flush_tools():
        if not buf:
            return
        names = [t.meta.get("name") for t in buf if t.meta.get("name")]
        names = list(dict.fromkeys(names))  # dedupe preserving order
        names_s = ", ".join(names) if names else "—"
        out.append(Text(f"  ····· tools ({len(buf)})  {names_s}",
                        style="#8E897F"))
        buf.clear()

    for t in tail:
        if t.role in ("tool_use", "tool_result"):
            buf.append(t)
            continue
        flush_tools()
        if t.role == "user":
            if last_role != "user":
                out.append(Text(""))
            body = Text(t.text, style="#C15F3C bold")
            out.append(body)
        elif t.role == "assistant":
            if last_role != "assistant":
                out.append(Text(""))
            out.append(Text(t.text, style="#EDE6D9"))
        last_role = t.role
    flush_tools()
    return out


# ---------- app ----------


class SessionDashTUI(App):
    CSS_PATH = "tui.tcss"
    TITLE = "session-dash"
    SUB_TITLE = "claude + codex"

    BINDINGS = [
        Binding("j,down", "cursor_down", "down", show=False),
        Binding("k,up", "cursor_up", "up", show=False),
        Binding("g", "top", "top"),
        Binding("G", "bottom", "end"),
        Binding("enter", "rejoin", "rejoin"),
        Binding("p", "pin", "pin"),
        Binding("slash", "focus_search", "search"),
        Binding("escape", "clear_search", "clear", show=False),
        Binding("r", "reindex", "reindex"),
        Binding("q", "quit", "quit"),
    ]

    query: reactive[str] = reactive("")
    sessions: reactive[list[dict]] = reactive(list, always_update=True)
    status: reactive[str] = reactive("loading…")
    selected_id: reactive[str | None] = reactive(None)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="/ to search — esc to clear", id="search")
        with Horizontal(id="panes"):
            table = DataTable(id="sessions", cursor_type="row", zebra_stripes=False)
            table.add_columns(" ", "tool", "title", "cwd", "when", "msgs")
            yield table
            yield RichLog(id="transcript", wrap=True, markup=False, highlight=False,
                          auto_scroll=False)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        init_db()
        search = self.query_one("#search", Input)
        search.display = False
        self.refresh_sessions()
        self.query_one(DataTable).focus()
        self.set_interval(30.0, self.refresh_sessions)

    # ---- actions ----

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_top(self) -> None:
        t = self.query_one(DataTable)
        if t.row_count:
            t.move_cursor(row=0)

    def action_bottom(self) -> None:
        t = self.query_one(DataTable)
        if t.row_count:
            t.move_cursor(row=t.row_count - 1)

    def action_focus_search(self) -> None:
        s = self.query_one("#search", Input)
        s.display = True
        s.focus()

    def action_clear_search(self) -> None:
        s = self.query_one("#search", Input)
        if s.has_focus or s.value:
            s.value = ""
            s.display = False
            self.query = ""
            self.refresh_sessions()
            self.query_one(DataTable).focus()

    def action_pin(self) -> None:
        sid = self._current_session_id()
        if not sid:
            return
        pinned = _toggle_pin(sid)
        self.status = f"{'pinned' if pinned else 'unpinned'} {sid[:8]}"
        self.refresh_sessions()

    def action_rejoin(self) -> None:
        row = self._current_row()
        if not row:
            return
        msg = _rejoin(row["tool"], row["id"], row["cwd"])
        self.status = msg

    @work(thread=True)
    def action_reindex(self) -> None:
        self.call_from_thread(setattr, self, "status", "reindexing…")
        stats = reindex(False)
        changed = stats["claude_new"] + stats["claude_updated"] + stats["codex_new"] + stats["codex_updated"]
        self.call_from_thread(setattr, self, "status",
                              f"reindex: {changed} changed")
        self.call_from_thread(self.refresh_sessions)

    # ---- events ----

    @on(Input.Changed, "#search")
    def on_search(self, event: Input.Changed) -> None:
        self.query = event.value.strip()
        self.refresh_sessions()

    @on(Input.Submitted, "#search")
    def on_search_submit(self) -> None:
        self.query_one(DataTable).focus()

    @on(DataTable.RowHighlighted)
    def on_row_highlight(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value if event.row_key else None
        self.selected_id = key
        self.render_transcript_for(self._row_by_id(key))

    # ---- data ----

    def refresh_sessions(self) -> None:
        prev_id = self._current_session_id()
        rows = _fetch_sessions(self.query or None)
        table = self.query_one(DataTable)
        table.clear()
        for r in rows:
            pin = Text("★", style="#C15F3C") if r["pinned"] else Text(" ")
            tool = Text(r["tool"],
                        style="#C15F3C" if r["tool"] == "claude" else "#0E7D5F")
            active = " •" if r["active"] else ""
            title = Text((r["ai_title"] or (r["first_prompt"] or "")[:80]) + active,
                         style="bold #EDE6D9" if r["active"] else "#EDE6D9")
            cwd = Text(short_cwd(r["cwd"]), style="#8E897F")
            when = Text((r["last_activity"] or "")[:16].replace("T", " "),
                        style="#8E897F")
            msgs = Text(str(r["message_count"] or 0), style="#8E897F")
            table.add_row(pin, tool, title, cwd, when, msgs, key=r["id"])
        self.sessions = rows
        self.status = f"{len(rows)} session{'s' if len(rows)!=1 else ''}" + (
            f" · search: {self.query}" if self.query else "")
        target = 0
        if prev_id:
            for idx, r in enumerate(rows):
                if r["id"] == prev_id:
                    target = idx
                    break
        if rows:
            table.move_cursor(row=target)
            self.render_transcript_for(rows[target])
        else:
            self.render_transcript_for(None)

    def render_transcript(self) -> None:
        self.render_transcript_for(self._current_row())

    def render_transcript_for(self, row: dict | None) -> None:
        log = self.query_one("#transcript", RichLog)
        log.clear()
        if not row:
            log.write(Text("select a session ←", style="#968a77 italic"))
            return
        title = row["ai_title"] or (row["first_prompt"] or "(untitled)")[:80]
        header = Text()
        header.append(f"{title}\n", style="bold #EDE6D9")
        header.append(f"{row['tool']}",
                      style="#C15F3C" if row["tool"] == "claude" else "#0E7D5F")
        header.append(" · ", style="#8E897F")
        header.append(f"{row.get('model') or '?'}", style="#8E897F")
        header.append(" · ", style="#8E897F")
        header.append(short_cwd(row.get("cwd")), style="#8E897F")
        log.write(header)
        log.write("")
        try:
            for part in _render_transcript(row["tool"], row["path"],
                                           row["mtime"] or 0.0):
                log.write(part)
        except Exception as e:
            log.write(Text(f"[error loading: {e}]", style="red"))

    # ---- helpers ----

    def _row_by_id(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        for r in self.sessions:
            if r["id"] == session_id:
                return r
        return None

    def _current_row(self) -> dict | None:
        return self._row_by_id(self.selected_id)

    def _current_session_id(self) -> str | None:
        return self.selected_id

    def watch_status(self, value: str) -> None:
        try:
            self.query_one("#status", Static).update(value)
        except Exception:
            pass


def main() -> None:
    SessionDashTUI().run()


if __name__ == "__main__":
    main()
