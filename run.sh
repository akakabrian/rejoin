#!/usr/bin/env bash
# Launch the rejoin server.
# Host/port come from ~/.config/rejoin/config.toml (see config.example.toml).
# Env vars REJOIN_HOST / REJOIN_PORT override the config file.
set -euo pipefail
cd "$(dirname "$0")"

read -r HOST PORT <<<"$(.venv/bin/python -c 'from rejoin.config import HOST, PORT; print(HOST, PORT)')"
HOST="${REJOIN_HOST:-$HOST}"
PORT="${REJOIN_PORT:-$PORT}"

exec .venv/bin/uvicorn rejoin.app:app --host "$HOST" --port "$PORT" "$@"
