#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HOST="${SESSION_DASH_HOST:-0.0.0.0}"
PORT="${SESSION_DASH_PORT:-8767}"
exec .venv/bin/uvicorn session_dash.app:app --host "$HOST" --port "$PORT" "$@"
