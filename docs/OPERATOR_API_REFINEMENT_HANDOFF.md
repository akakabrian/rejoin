# Rejoin Operator API Refinement Handoff

This handoff is for Codex to implement the final cleanup/refinement pass before starting the Related Sessions MVP.

The current operator substrate is strong: Rejoin now has file references, live session briefs, JSON session APIs, project summaries, diagnostics, inline structured search filters, and file-ref rebuilds. This pass should not add a large new product surface. It should tighten semantics, harden failure behavior, improve operator trust, and add tests around the new API primitives.

## Current State

Recent work added these major primitives:

- `rejoin/brief.py` for live session brief generation.
- `rejoin/search_query.py` for inline structured search filters.
- File-reference extraction and storage through `session_file_ref_events` and `session_file_refs`.
- `GET /api/sessions` for JSON session search/listing.
- `GET /api/sessions/{id}` for JSON session metadata.
- `GET /api/projects` for cwd/project summaries.
- `GET /api/sessions/{id}/tail` for a lightweight current-tail view.
- `GET /api/sessions/{id}/brief` for overall/progress/tail session briefs.
- `POST /reindex?force=true` support.
- `POST /api/sessions/{id}/file-refs/rebuild` for one-session file-ref rebuild.
- `GET /api/diagnostics` for schema/session/file-ref/provider counts.

This is enough to support a Hermes/Rejoin operator that can answer:

- What sessions exist?
- What is this session about?
- What progress has been made?
- Where is the session right now?
- What files are involved?
- Which projects/cwds have recent activity?
- Is the data live, indexed, stale, or unknown?

This pass should make those answers more reliable and less ambiguous.

---

## Goals

1. Make the tail endpoint degrade as gracefully as the brief endpoint.
2. Make time-sensitive API responses include generation time.
3. Make boolean inline search filters strict rather than silently coercing invalid values.
4. Add `active_count` to project summaries.
5. Add pagination support to `/api/sessions`.
6. Harden source-path checks and file-ref rebuild errors.
7. Add/expand tests for all newly added operator APIs and edge cases.
8. Preserve Rejoin's local-first, read-mostly, safe cache behavior.

---

## Non-Goals

Do not implement Related Sessions in this pass.

Do not add embeddings, vector search, LLM summarization, cloud sync, authentication, multi-user collaboration, or mutable upstream-agent session edits.

Do not rewrite the UI. Keep UI changes minimal unless required to support tests or API correctness.

---

## Required Changes

### 1. Make `/api/sessions/{id}/tail` use indexed fallback

Current behavior: if live/cached tail read fails, `_load_session_tail()` returns an empty tail with `tail_source = "unavailable"`.

Desired behavior: mirror `build_session_brief()` fallback behavior.

When live or cached tail loading fails:

- Set `tail_error` to a short error string.
- Add warning `tail_read_failed`.
- Set `tail_source = "indexed_fallback"`.
- Use `indexed_tail(row)` from `rejoin.brief`.
- Return fallback tail turns instead of an empty tail.

Implementation notes:

- Import `indexed_tail` from `rejoin.brief` into `app.py`, or expose a small helper from `brief.py` if preferred.
- Keep `ok: true`. A tail fallback is degraded, not a total endpoint failure.
- If indexed fallback has no turns, return an empty tail, but keep `tail_source = "indexed_fallback"` and `tail_error` populated.

Expected JSON shape:

```json
{
  "ok": true,
  "generated_at": "2026-06-02T...+00:00",
  "warnings": ["tail_read_failed"],
  "session_id": "...",
  "tail_source": "indexed_fallback",
  "tail_error": "...",
  "turn_count": 2,
  "tail": [
    {"index": 0, "role": "user", "text": "...", "meta": {"source": "indexed:first_prompt"}}
  ]
}
```

Acceptance criteria:

- A failed live read still returns indexed fallback content when `first_prompt`, `codex_summary`, or `last_prompt` exist.
- The endpoint returns `ok: true` on fallback.
- The endpoint includes `tail_read_failed` in warnings.
- The endpoint does not expose a traceback.

---

### 2. Add `generated_at` to tail responses

Add `generated_at: utcnow_iso()` to `_load_session_tail()` response.

Rationale: tail responses are live-ish and time-sensitive. Operators should know when the view was generated.

Acceptance criteria:

- `GET /api/sessions/{id}/tail` always returns `generated_at`.
- The value is an ISO timestamp string.

---

### 3. Make boolean inline filters strict

Current behavior: `active:maybe` or `pinned:nope` becomes false because anything not in the truthy set is coerced to false.

Desired behavior:

- Accept truthy values: `1`, `true`, `yes`, `on`.
- Accept falsy values: `0`, `false`, `no`, `off`.
- Invalid boolean filter values should not silently become false.

Recommended implementation:

In `rejoin/search_query.py`, add a helper:

```python
_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}

def _parse_bool_filter(value: str) -> bool | None:
    lowered = value.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    return None
```

When parsing `active:` or `pinned:`:

- If valid, store the bool.
- If invalid, do not set the structured filter.
- Preserve the original filter text in the remaining free-text query, or drop it and expose a warning.

Preferred simple behavior for now:

- Preserve invalid boolean tokens as normal text.

Example behavior:

```text
active:true bugfix      -> ParsedSearchQuery(active=True, q="bugfix")
active:false bugfix     -> ParsedSearchQuery(active=False, q="bugfix")
active:maybe bugfix     -> ParsedSearchQuery(active=None, q="active:maybe bugfix")
```

Acceptance criteria:

- `active:true` filters active sessions.
- `active:false` filters inactive sessions.
- `active:maybe` does not behave as `active:false`.
- Same for `pinned:true`, `pinned:false`, and invalid values.

---

### 4. Add `active_count` to `/api/projects`

Current project summaries include:

- `cwd`
- `label`
- `session_count`
- `file_ref_count`
- `last_activity`
- `tools`

Add:

```json
"active_count": 2
```

Implementation notes:

`active` is computed from both running IDs and last-activity recency. It is easier and safer to calculate in Python using the same `_is_active()` helper than to duplicate the active-window logic in SQL.

Suggested approach:

1. Fetch sessions grouped by cwd or fetch enough session rows needed to compute project summaries.
2. Use `_running_ids()` once.
3. For each row, compute active with `_is_active(row["last_activity"], now_epoch, running, row["id"])`.
4. Aggregate active counts per cwd.
5. Preserve the existing project fields.

Acceptance criteria:

- `/api/projects` includes `active_count` for every project row.
- A project with no active sessions returns `active_count: 0`.
- A session whose id is in `_running_ids()` counts as active even if `last_activity` is old.
- A recently active session counts as active according to `ACTIVE_WINDOW_SEC`.

---

### 5. Add offset pagination to `/api/sessions`

Current `/api/sessions` supports `limit` but not `offset`.

Add:

```python
offset: int = Query(0, ge=0)
```

Then update `_fetch_sessions()` to accept and apply offset:

```sql
LIMIT :limit OFFSET :offset
```

Implementation notes:

- Keep default `offset = 0`.
- HTML `/sessions` fragment can remain offset-free for now unless easy to share the same helper.
- JSON `/api/sessions` should pass both `limit` and `offset`.

Acceptance criteria:

- `/api/sessions?limit=10&offset=0` returns first page.
- `/api/sessions?limit=10&offset=10` returns next page.
- Existing callers without offset behave as before.

---

### 6. Harden `source_path_exists()` against `OSError`

Current `source_mtime()` catches `OSError`, but `source_path_exists()` can raise on unusual paths/permissions.

Update `source_path_exists()` in `rejoin/brief.py`:

```python
def source_path_exists(path: str | None) -> bool | None:
    if not path or "://" in path:
        return None
    try:
        return Path(path).exists()
    except OSError:
        return None
```

Acceptance criteria:

- Bad/unreadable paths do not crash brief/session warning generation.
- Virtual paths still return `None`.

---

### 7. Stable error codes for file-ref rebuild

Current `POST /api/sessions/{id}/file-refs/rebuild` returns raw exception text as `error` on failure.

Refine shape:

```json
{
  "ok": false,
  "error": "file_ref_rebuild_failed",
  "detail": "short exception string"
}
```

For not found, preserve:

```json
{"ok": false, "error": "not found"}
```

Implementation notes:

- Do not expose tracebacks.
- It is okay to include short exception detail because this is a local developer/operator tool.
- Consider logging the exception with `log.exception(...)`.

Acceptance criteria:

- Rebuild failures return stable `error: "file_ref_rebuild_failed"`.
- Failure response includes `detail` but no traceback.
- Successful response shape stays compatible.

---

### 8. Add warnings consistency where useful

Existing JSON session responses include warnings through `_session_json()` and brief responses include warnings. Tail responses also include warnings.

Make sure these warnings are consistent and operator-friendly:

Possible warning values:

- `source_path_missing`
- `freshness_unknown`
- `indexed_stale`
- `tail_read_failed`
- `file_refs_may_be_stale`

Acceptance criteria:

- Avoid one-off warning strings that duplicate the same meaning.
- Warnings are stable machine-readable strings.
- Brief markdown may keep human-readable freshness note, but JSON warnings should remain machine-friendly.

---

## Test Plan

Add or update tests for the following areas. Prefer focused unit tests for `brief.py` and `search_query.py`, plus API tests with `TestClient` for endpoint behavior.

### Search parser tests

File: likely `tests/test_search_query.py` or existing app/API test file.

Cases:

```text
parse_search_query('active:true bug')
  -> active=True, q='bug'

parse_search_query('active:false bug')
  -> active=False, q='bug'

parse_search_query('active:maybe bug')
  -> active=None, q includes 'active:maybe'

parse_search_query('pinned:on tool:codex cwd:"/tmp/proj" file:app.py op:edited')
  -> pinned=True, tool='codex', cwd='/tmp/proj', file='app.py', operation='edited'

file_filter_targets('app.py')
  -> (None, 'app.py')

file_filter_targets('rejoin/app.py')
  -> ('rejoin/app.py', None)
```

### Tail endpoint tests

Cases:

1. Live success:
   - `GET /api/sessions/{id}/tail?fresh=true&tail=2`
   - Returns `tail_source: "live"`.
   - Includes `generated_at`.
   - Includes 2 tail turns.

2. Cached/indexed mode:
   - `GET /api/sessions/{id}/tail?fresh=false`
   - Returns `tail_source: "indexed"` or current chosen cached source label.

3. Live failure fallback:
   - Monkeypatch `load_turns` or call path that raises.
   - Returns `ok: true`.
   - Returns `tail_source: "indexed_fallback"`.
   - Includes `tail_read_failed` warning.
   - Returns fallback turns from indexed fields if available.

4. `tail=0`:
   - Returns empty tail.
   - Does not crash.
   - Still returns `turn_count` and `generated_at`.

### Projects endpoint tests

Cases:

1. Basic project summary:
   - Returns cwd, label, session_count, file_ref_count, last_activity, tools.

2. Active count:
   - Monkeypatch `_running_ids()` to include one session id.
   - Assert matching project has `active_count >= 1`.

3. Recent activity active count:
   - Use last_activity inside `ACTIVE_WINDOW_SEC`.
   - Assert active_count includes it.

### API sessions pagination tests

Cases:

1. `GET /api/sessions?limit=1&offset=0` returns one row.
2. `GET /api/sessions?limit=1&offset=1` returns a different next row.
3. Existing filters still work with offset.

### File-ref rebuild tests

Cases:

1. Success:
   - Existing transcript contains paths.
   - POST rebuild returns `ok: true`, event_count, file_ref_count.

2. Failure:
   - Missing/invalid path or monkeypatched `load_turns` raises.
   - Response status 500.
   - JSON has `error: "file_ref_rebuild_failed"`.
   - JSON has `detail`.
   - No traceback in response.

### Brief/source robustness tests

Cases:

1. `source_path_exists()` catches `OSError` and returns `None`.
2. Virtual source path returns `None` for source mtime and path exists.
3. Brief response still includes warnings and source fields.

---

## Suggested Implementation Order

1. Update `brief.py`:
   - Harden `source_path_exists()`.
   - Ensure fallback helpers are reusable by tail endpoint.

2. Update `search_query.py`:
   - Strict boolean parsing.
   - Preserve invalid boolean filters as text.

3. Update `app.py` tail helper:
   - Add `generated_at`.
   - Use indexed fallback on read failure.

4. Update `/api/projects`:
   - Add `active_count` using `_is_active()` and `_running_ids()`.

5. Update `/api/sessions` pagination:
   - Add offset to API endpoint and `_fetch_sessions()`.
   - Keep HTML behavior unchanged unless easy.

6. Update file-ref rebuild error response:
   - Stable error code.
   - Short detail.
   - Log exception.

7. Add/expand tests.

8. Run:

```bash
ruff check rejoin tests
pytest -q
```

---

## Expected Final API Behavior

### Tail endpoint fallback

```http
GET /api/sessions/sess-1/tail?fresh=true&tail=12
```

If live read succeeds:

```json
{
  "ok": true,
  "generated_at": "2026-06-02T00:00:00+00:00",
  "warnings": [],
  "session_id": "sess-1",
  "tail_source": "live",
  "tail_error": null,
  "turn_count": 84,
  "tail": []
}
```

If live read fails but indexed fields exist:

```json
{
  "ok": true,
  "generated_at": "2026-06-02T00:00:00+00:00",
  "warnings": ["tail_read_failed"],
  "session_id": "sess-1",
  "tail_source": "indexed_fallback",
  "tail_error": "short error",
  "turn_count": 2,
  "tail": [
    {
      "index": 0,
      "role": "user",
      "text": "first prompt",
      "meta": {"source": "indexed:first_prompt"}
    }
  ]
}
```

### Project summaries

```http
GET /api/projects
```

```json
{
  "ok": true,
  "projects": [
    {
      "cwd": "/home/brian/src/rejoin",
      "label": "~/src/rejoin",
      "session_count": 12,
      "active_count": 1,
      "file_ref_count": 83,
      "last_activity": "2026-06-02T...",
      "tools": ["claude", "codex"]
    }
  ]
}
```

### Sessions pagination

```http
GET /api/sessions?limit=25&offset=25&q=tool:codex file:app.py
```

```json
{
  "ok": true,
  "sessions": []
}
```

### File-ref rebuild failure

```json
{
  "ok": false,
  "error": "file_ref_rebuild_failed",
  "detail": "short exception"
}
```

---

## Definition of Done

This pass is complete when:

- Tail endpoint degrades to indexed fallback instead of returning an empty unavailable tail on read failure.
- Tail endpoint includes `generated_at`.
- Invalid boolean structured filters no longer silently mean `false`.
- `/api/projects` includes `active_count`.
- `/api/sessions` supports offset pagination.
- `source_path_exists()` cannot crash on bad local paths.
- File-ref rebuild returns stable error codes.
- Tests cover the above cases.
- `ruff check rejoin tests` passes.
- `pytest -q` passes.

After this is complete, the next product feature should be Related Sessions MVP.
