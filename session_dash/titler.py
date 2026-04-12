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
    "You write concise titles for coding assistant sessions. "
    "Given excerpts from a session, respond with only a 5-8 word title in Title Case. "
    "No quotes, no punctuation at the end, no leading verbs like 'Discussing'. "
    "Focus on the task or topic. Examples: "
    "'QuickBooks Health Check Real Validation', 'Tailscale SSH Setup For Taro'."
)


def _content_for(row) -> str:
    fp = (row["first_prompt"] or "").strip()
    lp = (row["last_prompt"] or "").strip()
    summary = (row["codex_summary"] or "").strip()
    parts: list[str] = []
    if fp:
        parts.append(f"FIRST USER PROMPT:\n{fp[:2000]}")
    if summary:
        parts.append(f"SESSION SUMMARY:\n{summary[:1500]}")
    if lp and lp != fp:
        parts.append(f"LAST USER PROMPT:\n{lp[:800]}")
    return "\n\n".join(parts).strip()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


async def _generate_title(client: httpx.AsyncClient, api_key: str, content: str) -> tuple[str, int, int]:
    resp = await client.post(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/session-dash",
            "X-Title": "session-dash",
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
