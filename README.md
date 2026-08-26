# status_line

Real-time Claude Code status line that aggregates tokens across the main
session and all subagents. Replaces the previous bash+jq implementation
that only tracked main-session tokens.

## What it does

Renders a multi-line status string consumed by the Claude Code status-line
hook. Layout:

```
Session: <sid> | Branch: <git-branch> | Model: <model> | User: n/a | Context: 215K (107%)
|                                           in     out  cached
| start:                                   12k      1k       0
| sum:                                    1.1M     35k   50.9M
| main:                                   1.1M     30k   50.8M
| [ok]    Review: implementation plan      12k      4k    100k
| [err]   Review: quality                    0       0       0
| [run]   Task 4: MissingGlyphLog           3k      1k     12k
```

Line layout:

- Line 1 — header (`Session: ... | Context: NK (P%)`)
- Line 2 — table header (`in / out / cached`, each right-aligned under
  its own column)
- Line 3 — `start:` row with the FIRST assistant event's breakdown —
  the session's baseline message. A reference row: not included in the
  `sum:` aggregate.
- Line 4 — `sum:` row aggregating main + every agent across all three
  columns (omitted if there are zero agents)
- Line 5 — `main:` row with cumulative breakdown for the main session
- Lines 6+ — one row per agent, each with three numeric cells:
  input, output, cache-read. `cache_creation` tokens are tracked but
  NOT displayed.

Every table row (lines 2+) starts with the `| ` marker: Claude Code
strips leading whitespace from status-line rows, and the marker keeps the
all-spaces table-header row aligned with the rows below it.

Each numeric cell is formatted via `format_tokens` (so `1000` renders
as `1k`, `1_500_000` as `1.5M`).

| Tag      | Meaning                                              |
| -------- | ---------------------------------------------------- |
| `[ok]`   | last assistant event had `stop_reason=end_turn`      |
| `[err]`  | last assistant event had an API error marker         |
| `[stop]` | `meta.stoppedByUser=true` OR user event with         |
|          | `[Request interrupted by user]` marker               |
| `[run]`  | mid-flow (last assistant had `stop_reason=tool_use`) |

## Install

This module lives at `~/.claude/status_line/`. Claude Code invokes it
via the wrapper `status_line.sh`, which `exec`s `python3 status_line.py`.
No additional setup is required — the wrapper is referenced from Claude
Code's status-line hook configuration.

## Runtime dependencies

- **Python 3.9+** (only stdlib used: `json`, `os`, `subprocess`, `sys`,
  `pathlib`, `time`)
- **`git`** on `$PATH` (optional; absence is silently handled by
  returning `branch=""`)
- **Read access** to `~/.claude/projects/<encoded>/<sid>/{*.jsonl,subagents/}`
- **Write access** to `~/.claude/status_line/data/` (auto-created on
  first run)

## How it works

### Entry point

`main()` is called by Claude Code via the wrapper. It:

1. Reads the JSON hook payload from stdin.
2. Resolves `session_id` → ALL matching session dirs
   `~/.claude/projects/<encoded>/<sid>/` via `_resolve_session_dirs`
   (a session id can legitimately exist in several encoded project dirs —
   e.g. the main checkout and a worktree copy; the `transcript_path`'s
   dir comes first, the rest in glob order).
3. Computes cumulative tokens for the main jsonl (`compute_main_cum`),
   with on-disk cache at `~/.claude/status_line/data/main_<sid>.json`.
4. Iterates subagent jsonl files across ALL resolved dirs, computing a
   per-agent snapshot (`compute_agent_snapshot`) and merging them with
   dedup by `agentId` (first dir wins), using the agents cache at
   `~/.claude/status_line/data/agents_<sid>.json`.
5. Sorts agents by main-jsonl `tool_use` position (`sort_agents`).
6. Renders the multi-line output (`render_output`).
7. Prints to stdout. Never returns non-zero.

### Caching

Two cache files are persisted under `~/.claude/status_line/data/`:

| File                | Invalidation key                       | Purpose                                                    |
| ------------------- | -------------------------------------- | ---------------------------------------------------------- |
| `main_<sid>.json`   | `last_uuid` (tail of main jsonl)       | cumulative totals + first-message `start_*` + tool_use ids |
| `agents_<sid>.json` | `(last_uuid, mtime_jsonl, mtime_meta)` | per-agent render-ready snapshot dict                       |

Both main-cache field groups added after the first release
(`context_tokens`, `start_in`/`start_out`/`start_cached`) are part of the
cache-hit check: a pre-upgrade cache file that matches the key but lacks
them is treated as a miss and rescanned once, then rewritten in the new
shape.

Each per-agent entry in `agents_<sid>.json` is keyed by `agentId` and
holds the fields `last_uuid`, `mtime_jsonl`, `mtime_meta`, `status`,
`tokens_in`, `tokens_out`, `tokens_cached`, `description`, `toolUseId`.
The three `tokens_*` fields are the breakdown columns rendered in the
status line (input / output / cache-read). Cache-hit requires all three
fields to be present — a pre-upgrade cache missing any of them
invalidates and triggers a forward re-parse (see Edge cases).

Both files are written atomically (`.tmp` → `os.replace()`).

### Edge cases handled

- **No stdin** (empty pipe): prints header with empty `session_id`,
  exits 0.
- **Invalid JSON on stdin**: same as above.
- **Missing session dir** (no `~/.claude/projects/.../<sid>/`): header
  only, exits 0.
- **Permission denied on jsonl**: cumulative reads fail → caller
  returns zero-valued result, render still works.
- **Broken cache file**: detected (`JSONDecodeError` or non-dict),
  deleted, recomputed.
- **Empty jsonl**: returns zeros across the board, no crash.
- **Subagent with zero assistant events**: status forced to `[err]`
  (or `[stop]` if `meta.stoppedByUser=true`); the agent line is still
  emitted with three zero cells in the breakdown columns (never
  skipped).
- **Pre-upgrade agents cache (no breakdown fields)**: cache-hit
  requires `tokens_in`/`tokens_out`/`tokens_cached` to be present; if
  any are missing, the entry is treated as a miss and the jsonl is
  re-scanned. After the first such re-scan the cache is rewritten
  with the new shape and subsequent calls hit cleanly. Without this
  check, a stale entry would render zeros (via `int(field or 0)`) until
  the next jsonl mutation.
- **Stale empty `agents_<sid>.json`** (written by pre-2026-08 versions
  that scanned only one session dir — the worktree-split bug): rewritten
  automatically on the next invocation once agents are found across all
  session dirs; no manual cleanup needed.
- **Same session id in multiple project dirs** (main checkout + worktree
  copy): all matching dirs are resolved, agents merged across them, and
  duplicates deduped by `agentId` — the `transcript_path`'s dir wins;
  without a `transcript_path`, glob order decides.

## Tests

Run all tests:

```bash
cd ~/.claude/status_line
python3 -m pytest tests/ -v
```

210+ tests cover: pure functions (`format_tokens`, `detect_status`,
`parse_stdin`), I/O helpers (`compute_main_cum`, `compute_agent_snapshot`,
`find_session_dir(s)`, `_resolve_session_dirs`, `sort_agents`,
`_write_agents_cache`), `render_output` (including the `start:` row),
`main()` end-to-end against a real session fixture — including the
multi-dir merge across duplicate session dirs (`tests/test_resolve_session_dirs.py`,
`tests/test_find_session_dir.py`) and a runtime smoke test — and the bash
wrapper.

### Real-session fixture

`tests/fixtures/real_session/` contains a copy of session
`f5044e4f-3e01-4330-be72-eb008a1d035e` (38 subagents) used by
`test_main_integration.py`. The directory is gitignored — populate it
after a fresh clone (see `tests/fixtures/real_session/README.md`).
