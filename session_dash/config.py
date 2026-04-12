from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

from dotenv import dotenv_values

HOME = Path.home()
CLAUDE_PROJECTS_ROOT = HOME / ".claude" / "projects"
CODEX_SESSIONS_ROOT = HOME / ".codex" / "sessions"

DATA_DIR = HOME / ".local" / "share" / "session-dash"
DB_PATH = DATA_DIR / "index.db"

CONFIG_PATH = HOME / ".config" / "session-dash" / "config.toml"
CRM_ENV_PATH = HOME / "AI" / "projects" / "Paa Prefab CRM" / ".env"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

DEFAULTS: dict = {
    "model": "qwen/qwen3-30b-a3b-instruct-2507",
    "title_concurrency": 8,
    "refresh_interval_sec": 60,
    "transcript_tail": 40,
    "active_window_sec": 120,
    "turn_cache_size": 16,
    "long_turn_lines": 30,
    "long_turn_chars": 1500,
    "host": "0.0.0.0",
    "port": 8767,
}


def _load_toml() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"warning: failed to load {CONFIG_PATH}: {e}", file=sys.stderr)
        return {}


_cfg: dict = {**DEFAULTS, **_load_toml()}

OPENROUTER_MODEL: str = _cfg["model"]
TITLE_CONCURRENCY: int = _cfg["title_concurrency"]
REFRESH_INTERVAL_SEC: int = _cfg["refresh_interval_sec"]
TRANSCRIPT_TAIL: int = _cfg["transcript_tail"]
ACTIVE_WINDOW_SEC: int = _cfg["active_window_sec"]
TURN_CACHE_SIZE: int = _cfg["turn_cache_size"]
LONG_TURN_LINES: int = _cfg["long_turn_lines"]
LONG_TURN_CHARS: int = _cfg["long_turn_chars"]
HOST: str = _cfg["host"]
PORT: int = _cfg["port"]


def openrouter_api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    if CRM_ENV_PATH.exists():
        vals = dotenv_values(CRM_ENV_PATH)
        return vals.get("OPENROUTER_API_KEY")
    return None


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
