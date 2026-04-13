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
    "You write headlines for past coding-assistant sessions. The user will "
    "later scan dozens of these at a glance, and the SOLE purpose of your "
    "headline is to help them RECALL what this specific session was for.\n\n"
    "Input structure:\n"
    "- USER'S FIRST PROMPT — their actual goal. Your PRIMARY signal.\n"
    "- SESSION SUMMARY (optional) — assistant's compaction summary, if any.\n"
    "- USER'S LATEST PROMPT (optional) — shows how the conversation evolved.\n"
    "- WORKING DIRECTORY (optional) — often hints at the project.\n"
    "- SESSION SIZE — message + tool-call counts hint at scope.\n\n"
    "Rules:\n"
    "- 3 to 6 words, Title Case.\n"
    "- Lead with a concrete noun phrase. Extract the MOST specific noun you "
    "  can find in the user's words: file names, tool names, error types, "
    "  feature names, library names, API endpoints, commands. Those are what "
    "  make a session recognizable weeks later.\n"
    "- Never start with a generic verb: 'Handling', 'Setting Up', "
    "  'Discussing', 'Creating', 'Implementing', 'Exploring', "
    "  'Understanding', 'Working On', 'Debugging'. Drop them; lead with "
    "  the thing they're about.\n"
    "- Drop articles (a, an, the) and filler prepositions (for, of, with, "
    "  in) unless the title reads wrong without them.\n"
    "- If the user pasted an error, name it. If they asked about a specific "
    "  file or feature, name it. If the cwd reveals a project name you can "
    "  use (e.g. 'my-crm'), you may include it.\n"
    "- NEVER invent context. Use only what's in the input.\n"
    "- Output the title only. No quotes, no trailing period, no prefix.\n\n"
    "FALLBACK RULE — apply when the input is too thin to identify a topic:\n"
    "  If the user's first prompt is a greeting, a ping, a one-word test, a "
    "  generic capability question ('what can you do?'), a yes/no, or any "
    "  other fragment that doesn't name a topic, file, feature, or error, "
    "  AND the summary/latest prompt don't rescue it — respond with exactly:\n"
    "    Brief Exchange\n"
    "  Do NOT try to guess. Do NOT echo the user's greeting as a title. "
    "  'Hey' is not a title. 'Testing' is not a title.\n\n"
    "GOOD titles (substantive input):\n"
    "  'fix the quickbooks token refresh health check' → QuickBooks Token Refresh Check\n"
    "  'how do I ssh into my box over tailscale'       → Tailscale SSH Keyless Access\n"
    "  'retry warning in http client leaks query param'→ HTTP Client Query String Redaction\n"
    "  '--resume flag silently fails on claude'        → Claude Resume Flag Silent Failure\n"
    "  'help me add pi sessions to my dashboard'       → Pi Session Indexer Provider\n\n"
    "FALLBACK cases (thin input → Brief Exchange):\n"
    "  'hey'                 → Brief Exchange\n"
    "  'are you there?'      → Brief Exchange\n"
    "  'test 123'            → Brief Exchange\n"
    "  'what can you do?'    → Brief Exchange\n"
    "  'yes'                 → Brief Exchange\n\n"
    "BAD titles to AVOID even when input is substantive:\n"
    "  'Handling The Session Resume Flow'   (generic verb 'Handling')\n"
    "  'Working On A Dashboard For Sessions' (generic verb + filler)\n"
    "  'Discussion About Authentication'    (vague; no specifics)\n"
    "  'Session About QuickBooks'           (vague; what about it?)"
)


def _content_for(row) -> str:
    """Build the LLM input. First prompt is the headline signal; other fields
    are context in case the first prompt alone is ambiguous."""
    fp = (row["first_prompt"] or "").strip()
    lp = (row["last_prompt"] or "").strip()
    summary = (row["codex_summary"] or "").strip()
    cwd = (row["cwd"] or "").strip() if "cwd" in row.keys() else ""
    msg_n = row["message_count"] if "message_count" in row.keys() else 0
    tool_n = row["tool_call_count"] if "tool_call_count" in row.keys() else 0

    parts: list[str] = []
    if fp:
        parts.append(f"USER'S FIRST PROMPT (primary signal):\n{fp[:3000]}")
    if summary:
        parts.append(f"SESSION SUMMARY (context only):\n{summary[:1500]}")
    if lp and lp != fp:
        parts.append(f"USER'S LATEST PROMPT (context only):\n{lp[:800]}")
    if cwd:
        parts.append(f"WORKING DIRECTORY: {cwd}")
    parts.append(f"SESSION SIZE: {msg_n or 0} messages, {tool_n or 0} tool calls")
    return "\n\n".join(parts).strip()


# Bump when the system prompt changes; invalidates cached titles so they
# regenerate with the new instructions.
PROMPT_VERSION = 3


def _is_native_hash(h: str | None) -> bool:
    """Content-hash sentinels set by provider-native-title flows (e.g. Hermes
    sets 'hermes-native'). Skip regeneration for these so we don't overwrite
    a real title with an OpenRouter guess."""
    return bool(h) and h.endswith("-native")


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
            SELECT s.id, s.tool, s.cwd, s.first_prompt, s.last_prompt,
                   s.codex_summary, s.message_count, s.tool_call_count,
                   t.content_hash as existing_hash
            FROM sessions s
            LEFT JOIN titles t ON t.session_id = s.id
            WHERE s.first_prompt IS NOT NULL AND s.first_prompt != ''
            ORDER BY s.last_activity DESC
            """
        ).fetchall()

    to_title = []
    for row in rows:
        # Provider-native titles (Hermes etc.) own their own title slot.
        if not force and _is_native_hash(row["existing_hash"]):
            continue
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
