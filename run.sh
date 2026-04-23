#!/usr/bin/env bash
# Launch the rejoin server.
# Host/port come from ~/.config/rejoin/config.toml (see config.example.toml).
# Env vars REJOIN_HOST / REJOIN_PORT override the config file.
set -euo pipefail
cd "$(dirname "$0")"

exec .venv/bin/python -m rejoin.app "$@"
