# CLAUDE.md

Architecture and design notes for `~/.claude/status_line/`.

## Module layout

`status_line.py` is the entire runtime (~1000 lines). Test files mirror
the public surface one function per test module.

| Layer             | Functions                                              | Pure? |
| ----------------- | ------------------------------------------------------ | ----- |
| format / parse    | `format_tokens`, `format_context`,                     | yes*  |
|                   | `resolve_context_limit`, `detect_status`,              |       |
|                   | `parse_stdin`                                          |       |
| read / compute    | `_read_last_event`, `_scan_main_jsonl`,                | no    |
|                   | `_atomic_write_json`, `_load_meta_dict`, `_meta_mtime` |       |
| cache / aggregate | `compute_main_cum`, `compute_agent_snapshot`,          | no    |
|                   | `find_session_dir`, `sort_agents`                      |       |
| render            | `render_output`                                        | yes   |
| orchestrator      | `main()`                                               | no    |

\* `resolve_context_limit` reads env `CLAUDE_CODE_CONTEXT_LIMIT` (like
`parse_stdin` reads `AI_USER`); `format_context` is fully pure.

Pure helpers (no I/O) are exercised directly in tests; the rest are
exercised through fixtures under `tests/fixtures/`. The orchestrator
has both unit coverage (via subprocess in `test_main_integration.py`)
and an end-to-end run on a real session.

`render_output` recognizes the status tags `("ok", "run", "err", "stop",
"kill")` via the `_STATUSES` tuple. `kill` is **not** produced by
`detect_status` (which returns only the first four) — it originates in
the orchestrator override in `_compute_agents` when a main-log
`<task-notification>` carries `<status>killed</status>` for the agent.

## Cache invalidation strategy

### `data/main_<sid>.json`

- **Key:** tuple `(last_uuid, mtime_jsonl)`. `last_uuid` catches new
  assistant events; `mtime_jsonl` catches new queue-operation events
  (e.g. `<task-notification>` for subagent completion) appended without
  a new assistant event.
- **Read:** `compute_main_cum` does a single forward scan to extract
  `last_uuid` AND stat the file for `mtime_jsonl`; if both match the
  cached values, return the cached payload (skipping the cache write).
- **Write:** on cache miss, recompute the `cum_*` fields
  (`cum_in / cum_out / cum_cache_create / cum_cache_read`) and atomically
  write via `.tmp` + `os.replace`. The cached payload also includes a
  `task_notifications: dict[<task-id>, {ok,kill,err}]` field extracted
  from `<task-notification>` queue-operation events during the same
  forward scan — consumed by the orchestrator override in
  `_compute_agents` — and a `context_tokens: int` field: the
  input + cache_creation + cache_read of the LAST assistant event
  (context-window occupancy), extracted in the same pass.

[deviation] Cache hit additionally requires `context_tokens` to be
present in the cached dict (same field-presence-guard pattern as the
agents cache's breakdown fields) — otherwise a pre-upgrade cache would
render `Context: 0K (0%)` for one cycle after upgrade.

[deviation] The previous implementation tail-scanned the jsonl first
(to detect cache hits cheaply) and then forward-scanned on miss. That
double-read on miss was simplified to a single forward scan; cache
hits no longer avoid the forward scan. For sub-MB session jsonl the
cost difference is negligible (microseconds), and the simpler design
is easier to reason about. If profiling later shows the forward scan
on cache hit as hot, we can reintroduce the tail-read optimization.

[deviation] `mtime_jsonl` was added to the cache key as part of the
subagent-status-via-queue-notifications plan. Without it, queue-events
appended to main jsonl while the main session stays idle would be
invisible to the orchestrator override (last_uuid unchanged → cache
hit → stale task_notifications returned).

### `data/agents_<sid>.json`

- **Key:** tuple `(last_uuid, mtime_jsonl, mtime_meta)` per agent.
- **Read:** `compute_agent_snapshot` checks all three keys against the
  current on-disk state; full cache hit returns the cached dict
  unchanged.
- **Write:** the orchestrator rebuilds the agents cache dict after
  computing all snapshots, then writes atomically.

[deviation] Cache key includes `mtime_meta` so that meta.json edits
(e.g. `stoppedByUser` added later, `description` corrected) invalidate
the cache even when the jsonl itself is unchanged. Without `mtime_meta`
in the key, agents would silently render stale `status`/`description`
fields after a meta-only update.

[deviation] Cache hit additionally checks that all three breakdown
fields (`tokens_in`, `tokens_out`, `tokens_cached`) are present in the
cached entry. Stale caches from a prior version that match the three
keys but lack the breakdown fields are treated as misses and
recomputed — otherwise the renderer would show three zeros for one
cycle after upgrade on any session with a pre-existing agents cache.

## Status priority and overrides

`detect_status(last_event, meta)` returns one of
`{"err", "stop", "ok", "run"}` with this priority (highest first):

1. `err` — last event is `assistant` AND has `error`, `isApiErrorMessage`,
   or `apiErrorStatus >= 400`.
2. `stop` — `meta.stoppedByUser=true` OR last event is `user` with
   `[Request interrupted by user]` marker in its content (string, list-
   of-blocks, or nested tool_result).
3. `ok` — last event is `assistant` with `stop_reason=end_turn`.
4. `run` — anything else (mid-flow).

[deviation] `compute_agent_snapshot` overrides the result when the jsonl
contains **zero assistant events at all**: status is forced to `err`
(or `stop` if `meta.stoppedByUser=true`), regardless of what
`detect_status` would return. The plan spec called for this signal —
"the agent never produced output" should be visually distinct from
"the agent is mid-flow".

[deviation] The orchestrator override in `_compute_agents` may additionally
set `status="kill"` when a main-log `<task-notification>` event has
`<status>killed</status>` AND the agent's `<task-id>` matches the agent's
filename stem (with `agent-` prefix stripped). See
`docs/plans/completed/20260824-subagent-status-via-queue-notifications.md`
for the full design. The override sits **below** the priority chain above:
queue cannot downgrade `err` or `stop` (guard), but overrides both `run`
(the bug fix) and `ok` (queue is more recent than end_turn).

## Header `Context:` field

The header line ends with `| Context: <N>K (<P>%)` after `User:`.

- **N (absolute):** context-window occupancy in whole thousands.
  Source priority in `_context_segment`:
  1. payload `context_window.total_input_tokens` (extracted by
     `parse_stdin` into `context_tokens`) when positive — the freshest
     value, provided by Claude Code itself ("live context window from the
     most recent API response", input + cache writes + cache reads per the
     statusline docs), and available even when no local session dir exists;
  2. otherwise the jsonl-derived `context_tokens` from
     `compute_main_cum` (occupancy at the last assistant event — same
     formula), 0 when neither source has data.
- **P (percent divisor):** `resolve_context_limit(model)` —
  env `CLAUDE_CODE_CONTEXT_LIMIT` (positive int) wins outright; else
  `"[1m]"` in the model display name (case-insensitive) → 1e6; else 2e5.
  Malformed/non-positive env values are ignored, not fatal.

## Disk-layout deviation

The plan spec describes the main jsonl path as
`session_dir / f"{sid}.jsonl"`. In production Claude Code, the main
jsonl is a **sibling** of the session directory, not a child:

```
~/.claude/projects/<encoded-project>/<sid>/       # session_dir
~/.claude/projects/<encoded-project>/<sid>.jsonl  # main jsonl — SIBLING
~/.claude/projects/<encoded-project>/<sid>/subagents/agent-*.jsonl
```

`main()` is the only place that knows this layout — it computes
`main_jsonl = session_dir.parent / f"{sid}.jsonl"` and feeds it to
`compute_main_cum`. All `compute_*` helpers are layout-agnostic.

## Git Bash wrapper

`status_line.sh` is a 4-line bash wrapper:

```bash
#!/usr/bin/env bash
exec python3 "$(cd "$(dirname "$0")" && pwd)/status_line.py" || exit 0
```

The `|| exit 0` is a hard safety net: if `python3` is missing or
the script fails, the status-line hook MUST NOT propagate a non-zero
exit to the parent Claude Code session (which would surface as a
visible error to the user). The Python `main()` itself has the same
contract — it catches all `Exception`s and exits 0 with a degraded
header.

## Test conventions

- **Pure helpers** (`format_tokens`, `detect_status`, `parse_stdin`):
  direct unit tests with parametrized inputs.
- **I/O helpers** (`compute_*`, `find_session_dir`, `sort_agents`):
  tests use `tmp_path` and copy/synthesize fixtures into it.
- **Orchestrator** (`main`): end-to-end tests via subprocess
  (`test_main_integration.py`). The subprocess runs with
  `HOME=<tmp>` and `USERPROFILE=<tmp>` overrides, plus a populated
  `tests/fixtures/real_session/` for full integration coverage.

### Real-session fixture

`tests/fixtures/real_session/` is gitignored (~14 MB). To run the
integration tests after a fresh clone, populate it per
`tests/fixtures/real_session/README.md`. Without it,
`test_main_integration.py` skip-checks fail.

## Deviations log

The plan in `docs/plans/completed/20260824-status-line-tokens-aggregation.md`
documents several intentional deviations; this file mirrors those and
adds review-time ones. Inline `[deviation]` markers in `status_line.py`
point to the relevant explanation.

- **2026-08-24** — switched to the tabular breakdown format
  (`in / out / cached` columns) and dropped the flat `sum: / main:`
  fields. `compute_agent_snapshot` now exposes `tokens_in`,
  `tokens_out`, `tokens_cached` instead of a single `tokens` field;
  `compute_main_cum` no longer exposes `total`. Render is now a table
  with header labels and per-column right-alignment. See plan
  `docs/plans/20260824-token-breakdown-table.md`.
- **2026-08-24** — added the header `Context: <N>K (<P>%)` field (see
  "Header `Context:` field" above). New pure helpers
  `resolve_context_limit` / `format_context`; `parse_stdin` grew a
  `context_tokens` key; `compute_main_cum` persists `context_tokens`
  with a field-presence guard on cache hit.
