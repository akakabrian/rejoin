# session-dash

Local dashboard for browsing and rejoining Claude Code + Codex sessions on this machine.

- Walks `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/*.jsonl`, indexes to SQLite.
- Auto-titles sessions via OpenRouter (`openai/gpt-5-mini`); API key read from `~/AI/projects/Paa Prefab CRM/.env`.
- Rejoin spawns a detached `tmux` session in the original cwd; UI shows the attach command.
- Background refresh every 60s; list polls every 30s (pauses while typing).
- Dark-on-Pampas aesthetic borrowed from Claude.ai (Fraunces / DM Sans / Source Serif 4 / IBM Plex Mono; Crail copper accent).

## Run

```
./run.sh                          # binds 0.0.0.0:8767 by default
SESSION_DASH_PORT=9000 ./run.sh   # override
```

Data lives at `~/.local/share/session-dash/index.db`.

## Shortcuts

`j`/`k` or arrows navigate · `Enter` rejoin · `p` pin · `/` focus search · `Esc` blur.
