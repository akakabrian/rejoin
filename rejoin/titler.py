from __future__ import annotations

import asyncio
import hashlib

import httpx

from .common import utcnow_iso
from .config import (
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    TITLE_CONCURRENCY,
    openrouter_api_key,
)
from .db import connect, refresh_fts

SYSTEM_PROMPT = (
    "You write punchy, specific headlines for past coding-assistant sessions. "
    "The user will later scan dozens of these at a glance, so the title must "
    "make THIS session distinguishable from the rest.\n\n"
    "The input is the user's FIRST prompt to the assistant (their actual goal "
    "for the session), optionally followed by a compaction summary or their "
    "latest prompt. The first prompt is the primary signal — write the title "
    "around THAT.\n\n"
    "Rules:\n"
    "- 3 to 6 words, Title Case.\n"
    "- Lead with a concrete noun phrase. Never start with a generic verb like "
    "'Handling', 'Setting Up', 'Discussing', 'Creating', 'Implementing', "
    "'Exploring', 'Understanding', 'Working On'. Drop them.\n"
    "- Drop articles (a, an, the) and filler prepositions (for, of, with, in) "
    "unless the title genuinely reads wrong without them.\n"
    "- Use concrete terms from the user's actual words — file names, tool "
    "names, error types, feature names — not abstract categories.\n"
    "- If the user pasted an error, name the error. If they asked about a "
    "specific file or feature, name it.\n"
    "- Output the title only. No quotes, no trailing period, no prefix.\n\n"
    "Examples of GOOD titles:\n"
    "  User asks to fix a broken QuickBooks token health check\n"
    "    → QuickBooks Health Check Validation\n"
    "  User asks how to SSH into their machine over Tailscale\n"
    "    → Tailscale SSH Keyless Access\n"
    "  User pastes a retry-warning log that leaks a query param\n"
    "    → HTTP Client Retry Warning Redaction\n"
    "  User describes building a dashboard to rejoin sessions\n"
    "    → Session Dashboard With Tmux Rejoin\n"
    "  User reports the '--resume' flag silently fails\n"
    "    → Claude Resume Flag Silent Failure\n\n"
    "Examples of BAD titles to AVOID:\n"
    "  'Handling The Session Resume Flow'   (generic verb 'Handling')\n"
    "  'Working On A Dashboard For Sessions' (generic verb + filler)\n"
    "  'Discussion About Authentication'    (vague; no specifics)\n"
    "  'Session About QuickBooks'           (vague; what about it?)"
)


def _content_for(row) -> str:
    """Build the LLM input. First prompt is the headline signal; the other
    fields are context in case the first prompt alone is ambiguous."""
    fp = (row["first_prompt"] or "").strip()
    lp = (row["last_prompt"] or "").strip()
    summary = (row["codex_summary"] or "").strip()
    parts: list[str] = []
    if fp:
        parts.append(f"USER'S FIRST PROMPT (primary signal):\n{fp[:2000]}")
    if summary:
        parts.append(f"SESSION SUMMARY (context only):\n{summary[:1500]}")
    if lp and lp != fp:
        parts.append(f"USER'S LATEST PROMPT (context only):\n{lp[:800]}")
    return "\n\n".join(parts).strip()


# Bump when the system prompt changes; invalidates cached titles so they
# regenerate with the new instructions.
PROMPT_VERSION = 2


def _content_hash(content: str) -> str:
    keyed = f"v{PROMPT_VERSION}:{content}"
    return hashlib.sha256(keyed.encode("utf-8")).hexdigest()[:16]


async def _generate_title(client: httpx.AsyncClient, api_key: str, content: str) -> tuple[str, int, int]:
    resp = await client.post(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/rejoin",
            "X-Title": "rejoin",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "max_tokens": 60,
            "temperature": 0.3,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = (data["choices"][0]["message"].get("content") or "").strip()
    text = raw.strip('"').strip("'").rstrip(".")
    if not text:
        raise RuntimeError(f"empty content in response: {data}")
    usage = data.get("usage", {}) or {}
    return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def _fallback_title(first_prompt: str | None) -> str:
    if not first_prompt:
        return "(untitled session)"
    text = " ".join(first_prompt.split())
    return text[:60] + ("…" if len(text) > 60 else "")


async def _title_one(
    client: httpx.AsyncClient,
    api_key: str,
    sem: asyncio.Semaphore,
    row,
) -> dict | None:
    content = _content_for(row)
    if not content:
        return None
    chash = _content_hash(content)
    async with sem:
        try:
            title, tin, tout = await _generate_title(client, api_key, content)
            return {
                "session_id": row["id"],
                "title": title,
                "content_hash": chash,
                "generated_at": utcnow_iso(),
                "tokens_in": tin,
                "tokens_out": tout,
            }
        except Exception as e:
            return {
                "session_id": row["id"],
                "title": _fallback_title(row["first_prompt"]),
                "content_hash": chash,
                "generated_at": utcnow_iso(),
                "tokens_in": 0,
                "tokens_out": 0,
                "_error": str(e),
            }


async def backfill_titles(force: bool = False, limit: int | None = None) -> dict:
    api_key = openrouter_api_key()
    if not api_key:
        return {"error": "no OPENROUTER_API_KEY found"}

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.first_prompt, s.last_prompt, s.codex_summary, s.message_count,
                   t.content_hash as existing_hash
            FROM sessions s
            LEFT JOIN titles t ON t.session_id = s.id
            WHERE s.first_prompt IS NOT NULL AND s.first_prompt != ''
            ORDER BY s.last_activity DESC
            """
        ).fetchall()

    to_title = []
    for row in rows:
        content = _content_for(row)
        if not content:
            continue
        if not force and row["existing_hash"] == _content_hash(content):
            continue
        to_title.append(row)
        if limit and len(to_title) >= limit:
            break

    if not to_title:
        return {"titled": 0, "skipped": len(rows), "errors": 0, "tokens_in": 0, "tokens_out": 0}

    sem = asyncio.Semaphore(TITLE_CONCURRENCY)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(_title_one(client, api_key, sem, r) for r in to_title))

    stats = {"titled": 0, "skipped": len(rows) - len(to_title), "errors": 0,
             "tokens_in": 0, "tokens_out": 0}
    with connect() as conn:
        for res in results:
            if res is None:
                continue
            if "_error" in res:
                stats["errors"] += 1
            else:
                stats["titled"] += 1
            stats["tokens_in"] += res["tokens_in"]
            stats["tokens_out"] += res["tokens_out"]
            conn.execute(
                """
                INSERT INTO titles (session_id, title, content_hash, generated_at, tokens_in, tokens_out)
                VALUES (:session_id, :title, :content_hash, :generated_at, :tokens_in, :tokens_out)
                ON CONFLICT(session_id) DO UPDATE SET
                    title = excluded.title,
                    content_hash = excluded.content_hash,
                    generated_at = excluded.generated_at,
                    tokens_in = excluded.tokens_in,
                    tokens_out = excluded.tokens_out
                """,
                {k: v for k, v in res.items() if not k.startswith("_")},
            )
        conn.commit()
        refresh_fts(conn)
    return stats


if __name__ == "__main__":
    import pprint
    pprint.pprint(asyncio.run(backfill_titles()))
