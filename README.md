# status_line

Real-time Claude Code status line that aggregates tokens across the main
session and all subagents. Replaces the previous bash+jq implementation
that only tracked main-session tokens.

## What it does

Renders a multi-line status string consumed by the Claude Code status-line
hook. With a `prices.json` present (see [Costs](#costs-pricesjson)) the
table carries two extra columns — `model` (the model id from the jsonl
events) and `cost` (money/credits spent) — and every group expands to one
row per model:

```
Session: <sid> | Branch: <git-branch> | Model: <model> | User: n/a | Context: 215K (107%)
|                                      model            in     out  cached          cost
| start:                                               12k      1k       0
| sum:                                 glm-5.3        1.1M     34k   50.8M  9.5k credits
|                                      kimi-k3        150k     40k    3.0M          $1.9
|                                      MiniMax-M3      500     200      3k           n/a
| main:                                glm-5.3        1.1M     30k   50.7M  9.4k credits
|                                      kimi-k3        150k     40k    3.0M          $1.9
| [ok]    Review: implementation plan  glm-5.3         12k      4k    100k  34.9 credits
| [err]   Review: quality              MiniMax-M3      500     200      3k           n/a
| [run]   Task 4: MissingGlyphLog                        0       0       0
```

Without `prices.json` both columns disappear and each group renders a
single totals row — the exact pre-cost layout:

```
Session: <sid> | Branch: <git-branch> | Model: <model> | User: n/a | Context: 215K (107%)
|                                           in     out  cached
| start:                                   12k      1k       0
| sum:                                    1.3M     74k   53.8M
| main:                                   1.2M     70k   53.7M
| [ok]    Review: implementation plan      12k      4k    100k
| [err]   Review: quality                  500     200      3k
| [run]   Task 4: MissingGlyphLog            0       0       0
```

Line layout:

- Line 1 — header (`Session: ... | Context: NK (P%)`)
- Line 2 — table header: `in / out / cached` right-aligned under their
  columns; with prices also `model` (left-aligned, between the
  description and `in`) and `cost` (right-aligned, after `cached`). The
  label/description column's header cell is empty.
- Line 3 — `start:` row with the FIRST assistant event's breakdown —
  the session's baseline message. A reference row: not included in the
  `sum:` aggregate, never carrying model/cost cells.
- `sum:` group (omitted if there are zero agents) — per-model merge of
  the main session and every agent; each model keeps its own row (no
  cross-model sums).
- `main:` group — cumulative breakdown of the main session, one row per
  model.
- One group per agent — `[<status>]` icon and description on the FIRST
  row of the group only. Totals are cumulative across ALL of the agent's
  events (not the last API call's usage); one row per model the agent
  used.

Per-model rows whose tokens are all zero (e.g. `<synthetic>` events)
are skipped; a group left with no rows after that — an agent with no
events, or one whose events are all zero-token — still renders ONE row
with three zero cells and an empty `model` cell. Groups (and therefore
agents) are never skipped.

`cache_creation` tokens are tracked but NOT displayed and not priced.

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

## Costs (prices.json)

The `model` and `cost` columns are opt-in: they appear only when a valid
prices file exists at `~/.claude/status_line/prices.json`. The file is
private (gitignored) — copy [`prices.example.json`](prices.example.json)
next to the module as a starting point:

```json
[
  {
    "model": "glm-5.3@api.z.ai",
    "in": 6.9,
    "out": 24,
    "cache": 1.7,
    "per": 10000,
    "units": "credits"
  },
  {
    "model": "kimi-k3",
    "in": 3,
    "out": 15,
    "cache": 0.3,
    "per": 1000000,
    "units": "$"
  }
]
```

Fields:

- `model` — the lookup key. Either a plain model id (`kimi-k3`) or
  `model@host` for provider-specific pricing: `host` is the hostname of
  the `ANTHROPIC_BASE_URL` env var the hook inherits from the Claude
  Code process (`https://api.z.ai/api/anthropic` → key
  `glm-5.3@api.z.ai`). Lookup order: `model@host` first, then the bare
  `model`. Duplicate keys: the last entry wins.
- `in` / `out` / `cache` — price per `per` tokens for input, output and
  cache-read respectively. Missing fields default to 0.
- `per` — token divisor, required: must be a number > 0.
- `units` — optional cost label. Its first character decides placement:
  non-alphanumeric glues as a prefix (`$8.1`), anything else appends
  after a space (`402 credits`); empty/missing → the bare number.

Cost of one per-model row =
`(in·p_in + out·p_out + cached·p_cache) / per`; `cache_creation` is not
priced (it is not displayed anywhere). The file is re-read on every
hook invocation.

What happens when parts are missing:

| Situation                                  | Result                                       |
| ------------------------------------------ | -------------------------------------------- |
| no `prices.json` / unreadable / bad JSON / | both columns absent — the plain `in / out /` |
| invalid entry (`per <= 0`, non-numeric     | `cached` layout, one totals row per group    |
| price, non-list, ...)                      |                                              |
| model known but not in the price file      | `n/a` in the cost cell                       |
| group with no models after zero-skip       | one zero row with an empty `model` cell      |

## Install

This module lives at `~/.claude/status_line/`. Claude Code invokes it
via the wrapper `status_line.sh`, which `exec`s `python3 status_line.py`.
No additional setup is required — the wrapper is referenced from Claude
Code's status-line hook configuration.

## Runtime dependencies

- **Python 3.9+** (only stdlib used: `json`, `os`, `re`, `subprocess`,
  `sys`, `time`, `urllib.parse`, `pathlib`, `typing`)
- **`git`** on `$PATH` (optional; absence is silently handled by
  returning `branch=""`)
- **`ANTHROPIC_BASE_URL`** env var (optional): the hook inherits it from
  the Claude Code process and uses its hostname for `model@host` price
  lookups (see [Costs](#costs-pricesjson)). Unset/invalid → only plain
  model keys match.
- **Read access** to `~/.claude/projects/<encoded>/<sid>/{*.jsonl,subagents/}`
  and (optionally) `~/.claude/status_line/prices.json`
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
6. Renders the multi-line output (`render_output`), wiring in
   `prices.json` and the `ANTHROPIC_BASE_URL` hostname when a prices
   file exists (see [Costs](#costs-pricesjson)).
7. Prints to stdout. Never returns non-zero.

### Caching

Two cache files are persisted under `~/.claude/status_line/data/`:

| File                | Invalidation key                       | Purpose                                                                          |
| ------------------- | -------------------------------------- | -------------------------------------------------------------------------------- |
| `main_<sid>.json`   | `last_uuid` (tail of main jsonl)       | cumulative totals + first-message `start_*` + per-model breakdown + tool_use ids |
| `agents_<sid>.json` | `(last_uuid, mtime_jsonl, mtime_meta)` | per-agent render-ready snapshot dict                                             |

Both main-cache field groups added after the first release
(`context_tokens`, `start_in`/`start_out`/`start_cached`, `per_model`)
are part of the cache-hit check: a pre-upgrade cache file that matches
the key but lacks them is treated as a miss and rescanned once, then
rewritten in the new shape.

Each per-agent entry in `agents_<sid>.json` is keyed by `agentId` and
holds the fields `last_uuid`, `mtime_jsonl`, `mtime_meta`, `status`,
`tokens_in`, `tokens_out`, `tokens_cached`, `models`, `description`,
`toolUseId`. The three `tokens_*` fields are the CUMULATIVE breakdown
columns rendered in the status line (input / output / cache-read, summed
over all of the agent's assistant events); `models` is the per-model
breakdown feeding the `model`/`cost` columns. Cache-hit requires all
four (`tokens_*` + `models`) fields to be present — a pre-upgrade cache
missing any of them invalidates and triggers a forward re-parse (see
Edge cases).

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
  requires `tokens_in`/`tokens_out`/`tokens_cached`/`models` to be
  present; if any are missing, the entry is treated as a miss and the
  jsonl is re-scanned. After the first such re-scan the cache is
  rewritten with the new shape and subsequent calls hit cleanly. Without
  this check, a stale entry would render zeros (via `int(field or 0)`)
  until the next jsonl mutation.
- **Stale empty `agents_<sid>.json`** (written by pre-2026-08 versions
  that scanned only one session dir — the worktree-split bug): rewritten
  automatically on the next invocation once agents are found across all
  session dirs; no manual cleanup needed.
- **Same session id in multiple project dirs** (main checkout + worktree
  copy): all matching dirs are resolved, agents merged across them, and
  duplicates deduped by `agentId` — the `transcript_path`'s dir wins;
  without a `transcript_path`, glob order decides.
- **Missing/invalid `prices.json`** (no file, broken JSON, non-list,
  invalid entry): `load_prices` returns `None` → the model/cost columns
  are absent, the plain layout renders. Same for an unset
  `ANTHROPIC_BASE_URL`: plain model keys still match, `@host` keys
  never do.

## Tests

Run all tests:

```bash
cd ~/.claude/status_line
python3 -m pytest tests/ -v
```

327+ tests cover: pure functions (`format_tokens`, `detect_status`,
`parse_stdin`), price helpers (`provider_host`, `load_prices`,
`price_for`, `compute_cost`, `format_cost`), I/O helpers
(`compute_main_cum`, `compute_agent_snapshot`, `find_session_dir(s)`,
`_resolve_session_dirs`, `sort_agents`, `_write_agents_cache`),
`render_table` and `render_output` (model/cost columns, per-model
groups, the `start:` row), `main()` end-to-end against a real session
fixture — including the multi-dir merge across duplicate session dirs
(`tests/test_resolve_session_dirs.py`, `tests/test_find_session_dir.py`)
and a runtime smoke test — and the bash wrapper.

### Real-session fixture

`tests/fixtures/real_session/` contains a copy of session
`f5044e4f-3e01-4330-be72-eb008a1d035e` (38 subagents) used by
`test_main_integration.py`. The directory is gitignored — populate it
after a fresh clone (see `tests/fixtures/real_session/README.md`).
