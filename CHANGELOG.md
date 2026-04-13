# Changelog

All notable changes to **rejoin** are documented here.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version numbers follow [Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-04-12

### Added
- **Hermes Agent** support via direct read of `~/.hermes/state.db` (SQLite).
  Shapes Hermes's sessions/messages tables into our SessionRecord + Turn
  model without writing to the DB.
- **Uses Hermes's native title** when present. Inserted into our `titles`
  table with content_hash `hermes-native` so the OpenRouter titler
  doesn't waste tokens regenerating.
- Resume: `hermes --resume <id>` (Hermes's native flag; fully interactive).
- Magenta `#7A3F74` tag color in both front-ends.

## [0.2.0] — 2026-04-12

### Added
- **OpenClaw** support via native JSONL parser at
  `~/.openclaw/agents/<agentId>/sessions/<sessionId>.jsonl` — Pi-based tree
  format with session-header line + typed message entries (text / toolCall /
  toolResult).
- `openclaw` tag color (rust-red `#B8432A`) in both web and TUI.
- Resume command: `openclaw agent --session-id <id> -m continue`.
  OpenClaw has no native interactive resume subcommand, so the follow-up
  message defaults to `continue` and the user can edit it inside the spawned
  tmux window.

### Notes
- Parser is schema-based against OpenClaw's documented format; tested with
  synthetic fixtures. Please file an issue if your real OpenClaw sessions
  don't parse — the JSONL format may differ in details we don't cover yet.

## [0.1.0] — 2026-04-12

### Added
- Two front-ends sharing one SQLite cache: a web dashboard (FastAPI + HTMX) with
  Claude.ai-inspired warm Pampas/Crail palette, and a terminal UI (Textual, tmux-aware).
- Support for four coding-agent session sources: Claude Code and Codex via our own
  parsers (richer detail), OpenCode and Pi via the [`agent-sessions`][as] library.
- Auto-titling via OpenRouter (`qwen/qwen3-30b-a3b-instruct-2507`) — ~$7e-6 per title;
  content-hash skip avoids redundant regeneration.
- Rejoin in tmux: one click / Enter spawns a detached session in the original `cwd`;
  inside tmux the TUI opens a new window in the current server instead.
- Incremental reindex every 60 s (mtime skip), FTS5 search with highlighting, pin
  favorites (★ floats to top), group-by-cwd, active-session pulse via mtime window
  OR ps-scan of running `--resume` processes.
- Keyboard-first: `j`/`k`/`Enter`/`p`/`/`/`t`/`?`/`q` in both front-ends.
- TOML config at `~/.config/rejoin/config.toml`; every knob has a default.
- Schema-version guard on the SQLite cache; friendly error on migration mismatch.
- 27 pytest cases, ruff config, GitHub Actions CI on Python 3.11 + 3.12.
- Console scripts: `rejoin` (web), `rejoin-tui` (terminal).

[as]: https://github.com/larsderidder/agent-sessions
