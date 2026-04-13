# Contributing

This is a small personal tool that other people might find useful. Keep that in mind for PRs — it's intentionally not a framework.

## Shape of the project

- **Runs locally.** No auth, no hosting, no container. The web and TUI front-ends share a SQLite cache at `~/.local/share/rejoin/index.db`.
- **Minimal deps.** The runtime stack is `fastapi + uvicorn + jinja2 + httpx + python-dotenv + textual`. Adding a dep should have an obvious payoff.
- **Readers only.** The indexer and titler only read from `~/.claude/projects/` and `~/.codex/sessions/`. We never modify session files.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh          # web, :8767
./run-tui.sh      # terminal (tmux-aware)
```

## Running the indexer / titler directly

```bash
.venv/bin/python -m rejoin.indexer
.venv/bin/python -m rejoin.titler
```

Both print a stats dict on exit.

## Testing notes

There is no test suite yet. Manual smoke-test flow:

1. `./run.sh` + visit the dashboard → filter by tool/cwd/search → click a session → verify transcript.
2. Pin a row, toggle `grouped`, confirm the pinned group appears at the top.
3. Click "rejoin in tmux", verify a `sess-<tool>-<uuid8>` tmux session spawns with the right cwd.
4. `./run-tui.sh` → `j/k/Enter/p/` bindings all behave.

If you add functionality, consider noting a smoke-test step above.

## Style

- Python 3.11+ only (uses `tomllib`, `from __future__ import annotations`).
- Shared helpers live in `rejoin/common.py`. Before adding a new utility, check there first.
- Tool-specific logic (Claude-only vs Codex-only) belongs behind the `PARSERS` registry in `indexer.py` or the `_ITERATORS` registry in `transcript.py`. Avoid `if tool == "claude"` branches outside those tables.
- The web UI uses HTMX for interactivity; avoid JS frameworks.
- CSS tokens live at the top of `static/style.css`; the TCSS for the TUI is in `rejoin/tui.tcss`.

## Adding a new harness

To index sessions from another agent harness:

1. Write `parse_X_session(path) -> SessionRecord | None` in `indexer.py`; add to `PARSERS`.
2. Write `iter_X_turns(path) -> Iterator[Turn]` in `transcript.py`; add to `_ITERATORS`.
3. Add the resume command in `resume.py`.
4. Add a tag color in `static/style.css` and `rejoin/tui.py`.

## Filing issues / PRs

Issues welcome. For PRs, keep them small and include a manual smoke-test note. Features that require servers, authentication, or cloud dependencies are out of scope — this tool stays local.
