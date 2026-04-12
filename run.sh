#!/usr/bin/env bash
# Launch the session-dash server.
# Host/port come from ~/.config/session-dash/config.toml (see config.example.toml).
# Env vars SESSION_DASH_HOST / SESSION_DASH_PORT override the config file.
set -euo pipefail
cd "$(dirname "$0")"

read -r HOST PORT <<<"$(.venv/bin/python -c 'from session_dash.config import HOST, PORT; print(HOST, PORT)')"
HOST="${SESSION_DASH_HOST:-$HOST}"
PORT="${SESSION_DASH_PORT:-$PORT}"

exec .venv/bin/uvicorn session_dash.app:app --host "$HOST" --port "$PORT" "$@"
