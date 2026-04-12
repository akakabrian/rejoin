# session-dash tutorial

A narrative first-run. Assumes you already have `claude` or `codex` (or both) installed and have run at least one session with either of them.

## 1. First launch

```bash
cd ~/AI/tools/session-dash
./run.sh
```

Open `http://127.0.0.1:8767/`. The first thing that happens is an index pass — the server walks `~/.claude/projects/` and `~/.codex/sessions/`, reads one pass through each `*.jsonl`, and writes a row per session to `~/.local/share/session-dash/index.db`. On a fresh machine with ~30 sessions this takes a second or two.

Then the background titler kicks off. You'll see placeholder titles (the first 80 chars of the first user prompt) gradually replaced by real ones like `QuickBooks Health Check Real Validation` — that's `qwen/qwen3-30b-a3b-instruct-2507` writing a 5–8 word summary from the session's first prompt + Codex compaction summary (if any) + last prompt. The initial backfill for ~30 sessions costs fractions of a cent.

While all that's happening, the UI is fully interactive — click any row.

## 2. Reading a transcript

The right pane uses typography to signal who's talking:

- **User** = warm serif on a copper-tinted block, left-ruled in copper. That's your voice.
- **Assistant** = quiet body serif on the paper background, ruled in a thin grey line.
- **Tool calls** = everything in between collapses into a single `····· tools  5  Write, Edit` row. Click it to expand and see each call's arguments and return value.

Long turns (>30 lines or >1500 chars) fade at the bottom and show a pill button: `↓ expand · 47 lines`. Click to read the rest.

Very long transcripts (>40 turns) show only the last 40 with a `load 232 earlier turns (272 total)` button at the top.

## 3. Rejoining a session

Pick a session you want to pick up. Two options:

**Click "rejoin in tmux"** (the copper pill button).

The server runs:

```bash
tmux new-session -d -s sess-claude-dbdd414a 'cd /home/brian/AI/projects/Paa\ Prefab\ CRM && claude --resume dbdd414a-…'
```

The UI shows:

> tmux session `sess-claude-dbdd414a` started. attach: `tmux attach -t sess-claude-dbdd414a`

Paste the attach command in any terminal (or run it over SSH/Tailnet if you're remoting in) and you're back in the conversation exactly where you left off.

**Or: click "copy command"** to get the raw `cd … && claude --resume …` string to paste wherever you want — useful if you don't like tmux or want to paste it into a different multiplexer.

### Keyboard-only flow

`j` / `k` to scan rows, `Enter` to rejoin. That's the entire interaction; you never have to touch the mouse.

## 4. Pinning and organizing

You'll accumulate a lot of sessions. Three ways to cut through the noise:

- **Search** — press `/`, type. FTS5 runs over titles, first/last prompts, Codex summaries. Matches are highlighted in amber. Works in real time (300ms debounce on keystroke).
- **Filter by tool or cwd** — the two dropdowns in the header. Great for "just show me my Paa Prefab CRM work."
- **Group by cwd** — check the `grouped` toggle. Sessions nest under sticky project headers, with a `★ pinned` group at the very top.

Pinning: click the tiny ★ in any row's left column (it's faint beige until you hover the row, where it turns grey; clicking turns it full amber). Pinned sessions sort to the top across all views and filters. Press `p` to toggle the pin on the currently-open session.

## 5. When sessions update

The background loop runs every 60 seconds:

1. Rescan both session directories, skipping files whose `mtime` hasn't changed (cheap).
2. If anything changed, reparse those files, update the DB, rebuild the FTS index.
3. If any titles need regenerating (new content → new content hash), titler pass.

The list auto-polls every 30 seconds (pausing while you're typing in search). The `indexed Ns ago` label in the header confirms the refresh loop is alive. If you want an immediate kick, click the **↻** icon.

## 6. Config

Say you want shorter transcripts and a smaller active window. Create `~/.config/session-dash/config.toml`:

```toml
transcript_tail   = 20
active_window_sec = 60
```

Restart the server. All other values stay at their defaults.

See [`config.example.toml`](../config.example.toml) for the full list.

## 7. Throwing it away

session-dash is a pure cache layer. Nothing it writes is authoritative.

- Delete `~/.local/share/session-dash/index.db` → next launch rebuilds everything from scratch. Titles will re-gen (costs a few cents).
- Delete `~/.config/session-dash/config.toml` → back to defaults.
- Delete the project directory → nothing else is affected. Your Claude and Codex session files are untouched.
