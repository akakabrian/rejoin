from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

from dotenv import dotenv_values

HOME = Path.home()
CLAUDE_PROJECTS_ROOT = HOME / ".claude" / "projects"
CODEX_SESSIONS_ROOT = HOME / ".codex" / "sessions"
OPENCLAW_AGENTS_ROOT = HOME / ".openclaw" / "agents"

DATA_DIR = HOME / ".local" / "share" / "rejoin"
DB_PATH = DATA_DIR / "index.db"

CONFIG_PATH = HOME / ".config" / "rejoin" / "config.toml"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ENV_PATH = PROJECT_ROOT / ".env"

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
    # Loopback by default: the dashboard exposes your full transcript
    # history. Change to "0.0.0.0" only on machines where you trust
    # every peer on the network (e.g. a Tailnet-only host).
    "host": "127.0.0.1",
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
    """Resolve the OpenRouter key from (in order):

    1. $OPENROUTER_API_KEY
    2. A .env at the project root (gitignored; create if you want)
    3. An .env file pointed to by $OPENROUTER_ENV_FILE (useful if you
       already keep the key in another project's .env)
    """
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    if PROJECT_ENV_PATH.exists():
        key = dotenv_values(PROJECT_ENV_PATH).get("OPENROUTER_API_KEY")
        if key:
            return key
    env_file = os.environ.get("OPENROUTER_ENV_FILE")
    if env_file:
        path = Path(env_file).expanduser()
        if path.exists():
            return dotenv_values(path).get("OPENROUTER_API_KEY")
    return None


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
