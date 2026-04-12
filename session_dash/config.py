from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

HOME = Path.home()
CLAUDE_PROJECTS_ROOT = HOME / ".claude" / "projects"
CODEX_SESSIONS_ROOT = HOME / ".codex" / "sessions"

DATA_DIR = HOME / ".local" / "share" / "session-dash"
DB_PATH = DATA_DIR / "index.db"

CRM_ENV_PATH = HOME / "AI" / "projects" / "Paa Prefab CRM" / ".env"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "openai/gpt-5-mini"

TITLE_MAX_WORDS = 8
TITLE_CONCURRENCY = 8


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
