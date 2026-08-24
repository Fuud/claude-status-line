# CLAUDE.md

Architecture and design notes for `~/.claude/status_line/`.

## Module layout

`status_line.py` is the entire runtime (~900 lines). Test files mirror
the public surface one function per test module.

| Layer             | Functions                                              | Pure? |
| ----------------- | ------------------------------------------------------ | ----- |
| format / parse    | `format_tokens`, `detect_status`, `parse_stdin`        | yes   |
| read / compute    | `_read_last_event`, `_scan_main_jsonl`,                | no    |
|                   | `_atomic_write_json`, `_load_meta_dict`, `_meta_mtime` |       |
| cache / aggregate | `compute_main_cum`, `compute_agent_snapshot`,          | no    |
|                   | `find_session_dir`, `sort_agents`                      |       |
| render            | `render_output`                                        | yes   |
| orchestrator      | `main()`                                               | no    |

Pure helpers (no I/O) are exercised directly in tests; the rest are
exercised through fixtures under `tests/fixtures/`. The orchestrator
has both unit coverage (via subprocess in `test_main_integration.py`)
and an end-to-end run on a real session.

## Cache invalidation strategy

### `data/main_<sid>.json`

- **Key:** `last_uuid` of the most recent assistant event in the main
  jsonl.
- **Read:** compute_main_cum does a single forward scan to extract
  `last_uuid`; if it matches the cached value, return the cached
  payload (skipping the cache write).
- **Write:** on cache miss, recompute totals and atomically write
  via `.tmp` + `os.replace`.

[deviation] The previous implementation tail-scanned the jsonl first
(to detect cache hits cheaply) and then forward-scanned on miss. That
double-read on miss was simplified to a single forward scan; cache
hits no longer avoid the forward scan. For sub-MB session jsonl the
cost difference is negligible (microseconds), and the simpler design
is easier to reason about. If profiling later shows the forward scan
on cache hit as hot, we can reintroduce the tail-read optimization.

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
