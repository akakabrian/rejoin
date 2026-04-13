# Changelog

All notable changes to **rejoin** are documented here.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
version numbers follow [Semantic Versioning](https://semver.org/).

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
