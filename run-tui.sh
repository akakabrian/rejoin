#!/usr/bin/env bash
# Launch the terminal UI. Safe to run inside tmux (rejoin opens a new window).
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m rejoin.tui "$@"
