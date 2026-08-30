# status_line

Real-time Claude Code status line that aggregates tokens across the main
session and all subagents. Replaces the previous bash+jq implementation
that only tracked main-session tokens.

## What it does

Renders a multi-line status string consumed by the Claude Code status-line
hook. Three duration columns — `work`, `wait`, `total` (see
[Time columns](#time-columns-work--wait--total)) — are ALWAYS visible and
close every table row in both layouts. With a `prices.json` present (see
[Costs](#costs-pricesjson)) the table additionally carries two extra
columns — `model` (the model id from the jsonl events) and `cost`
(money/credits spent) — and every group expands to one row per model:

```
Session: <sid> | Branch: <git-branch> | Model: <model> | User: n/a | Context: 215K (107%)
|                                      model            in     out  cached  cost           work  wait total
| start:                               glm-5.3         12K      1K       0  10.7 credits
| sum:                                 glm-5.3        1.1M     34K   50.8M  9.5K credits  28:03 00:08 28:11
|                                      kimi-k3        150K     40K    3.0M  $1.9
|                                      MiniMax-M3      500     200      3K   n/a
| main:                                glm-5.3        1.1M     30K   50.7M  9.4K credits  28:03 00:08 28:11
|                                      kimi-k3        150K     40K    3.0M  $1.9
| [ok]    Review: implementation plan  glm-5.3         12K      4K    100K  34.9 credits  01:12 00:03 01:15
| [err]   Review: quality              MiniMax-M3      500     200      3K   n/a          00:26 00:05 00:31
| [run]   Task 4: MissingGlyphLog                        0       0       0                00:02 00:00 00:02
```

Without `prices.json` both columns disappear, each group renders a single
totals row — but the always-visible `work / wait / total` time columns
stay:

```
Session: <sid> | Branch: <git-branch> | Model: <model> | User: n/a | Context: 215K (107%)
|                                           in     out  cached   work  wait total
| start:                                   12K      1K       0
| sum:                                    1.3M     74K   53.8M  28:03 00:08 28:11
| main:                                   1.2M     70K   53.7M  28:03 00:08 28:11
| [ok]    Review: implementation plan      12K      4K    100K  01:12 00:03 01:15
| [err]   Review: quality                  500     200      3K  00:26 00:05 00:31
| [run]   Task 4: MissingGlyphLog            0       0       0  00:02 00:00 00:02
```

Line layout:

- Line 1 — header (`Session: ... | Context: NK (P%)`)
- Line 2 — table header: `in / out / cached` right-aligned under their
  columns; with prices also `model` (left-aligned, between the
  description and `in`) and `cost` (right-aligned, after `cached`). The
  three time labels `work / wait / total` close the header in BOTH
  layouts. The label/description column's header cell is empty.
- Line 3 — `start:` row with the FIRST assistant event's breakdown —
  the session's baseline message. A reference row: not included in the
  `sum:` aggregate; its three time cells are always empty; in prices
  mode it carries that event's model and its priced cost.
- `sum:` group (omitted if there are zero agents) — per-model merge of
  the main session and every agent; each model keeps its own row (no
  cross-model sums). Its time cells are the SESSION's union triple.
- `main:` group — cumulative breakdown of the main session, one row per
  model. Same session time triple as `sum:` — waiting on agents already
  counts as main's work (see [Time columns](#time-columns-work--wait--total)).
- One group per agent — `[<status>]` icon and description on the FIRST
  row of the group only. Totals are cumulative across ALL of the agent's
  events (not the last API call's usage); one row per model the agent
  used. The agent's personal work/wait/total render on that first row;
  continuation per-model rows leave the time cells blank.

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
as `1K`, `1_500_000` as `1.5M`) and each duration cell via
`format_duration` (`HH:MM`). Every column's width — including the
`sum:` row's cells — is the widest cell under it (floored at 7 for the
token columns, at 5 for the duration columns — `HH:MM` fills that
exactly), so at extreme totals the columns can be one character
wider than the other rows suggested.

| Tag      | Meaning                                              |
| -------- | ---------------------------------------------------- |
| `[ok]`   | last assistant event had `stop_reason=end_turn`      |
| `[err]`  | last assistant event had an API error marker         |
| `[stop]` | `meta.stoppedByUser=true` OR user event with         |
|          | `[Request interrupted by user]` marker               |
| `[run]`  | mid-flow (last assistant had `stop_reason=tool_use`) |

The `[err]` markers (`error` / `isApiErrorMessage` / `apiErrorStatus>=400`)
are looked up BOTH inside `message` (legacy event shape) AND at the event
top level: Claude Code 2.1.224 writes the synthetic API-error event (e.g.
a 429 death) with the markers as siblings of `message` and
`stop_reason="stop_sequence"` — without the top-level lookup such dead
agents classify as `[run]` forever.

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
  non-alphanumeric glues as a prefix inside the cost cell (`$8.1`);
  anything else renders in a separate unlabeled column after `cost`
  (`402` + `crds`), so the cost column's numbers right-align; empty or
  missing → the bare number.

Number formatting of the cost cell: `>= 1M` → `X.XM`, `>= 1000` →
`X.XK`, `>= 0.1` → one decimal with a trailing `.0` dropped (`402`),
otherwise two decimals (`0.04`).

Cost of one per-model row =
`(in·p_in + out·p_out + cached·p_cache) / per`; `cache_creation` is not
priced (it is not displayed anywhere). The file is re-read on every
hook invocation and must be plain UTF-8 or UTF-8 with BOM.

What happens when parts are missing:

| Situation                             | Result                                  |
| ------------------------------------- | --------------------------------------- |
| no / unreadable / invalid prices file | both columns absent — the plain layout  |
| model known but not in the price file | `n/a` in the cost cell                  |
| group with no models after zero-skip  | one zero row with an empty `model` cell |

## Time columns (work / wait / total)

The last three columns measure wall-clock time and are present in BOTH
layouts (plan 20260827-status-line-time-columns):

- `total` — elapsed session wall-clock: `now − first_ts`, where
  `first_ts` is the FIRST timestamped event in the main jsonl (ISO 8601
  stamps parsed to epoch seconds; a trailing `Z` is handled by hand for
  Python 3.9).
- `work` — autonomous time: the UNION of all active intervals — the main
  session's turns plus every subagent's active lifetime — minus
  AskUserQuestion pauses. Waiting on a running agent counts as WORK
  (it is the machine doing what the user asked); parallel agents do NOT
  double-count overlapping wall-clock time (union, not a sum).
- `wait` — user-facing idle: `total − work` (clamped at 0). Includes
  the current unfinished pause.

By construction `work + wait = total` on the session rows (`sum:` /
`main:`). An AGENT row is exempt while one of its questions hangs
unanswered: its `total` freezes at the question moment, but the open gap
keeps growing its `wait` — so `wait > total` there is the honest picture
(the work the agent performed before asking is never erased).

How intervals are derived:

- **Turns** — the main scan splits the session at "real" user events
  (`type=user` with string content: prompts, commands, interrupts;
  `tool_result` lists do NOT bound anything). A turn spans from its
  prompt to the last activity carrying a timestamp; trailing
  `queue-operation`/`system` events don't extend it — a notification
  about a background agent must not shift the start of the wait.
- **AskUserQuestion pauses** — from an assistant event carrying an
  AskUserQuestion `tool_use` until the next user event of any kind.
  A pause is cut out of BOTH the containing turn's work and the asking
  agent's lifetime. While a question hangs UNANSWERED nothing accrues:
  the gap grows as wait, and the turn counts as closed (no live-now
  extension through it).
- **Live-now** — when the last turn is still open (the session ended in
  `tool_use`/`pause_turn`, tool results follow the last assistant event,
  or the last real prompt simply has no assistant reply yet) and when an
  agent's status is `[run]` without a hanging question, their LAST
  interval stretches up to the render moment — `total` grows in real time
  without new jsonl writes. A turn closes on `end_turn`, a
  `stop_sequence`, an interrupt, an error, or a hanging question; an
  assistant reply WITHOUT a `stop_reason` (an aborted/truncated
  generation) also counts as closed — the plan's "closed on error" rule,
  which bounds the damage of a crash to an undercount instead of counting
  unbounded dead air as work.
- **Agents** — each agent renders its own triple: `total` = lifetime
  (first → last stamped event, extended to now while running, frozen at
  the question moment while one hangs);
  `work` = `total` − Σ clipped closed AskUserQuestion pauses;
  `wait` = those closed pauses plus the OPEN question's gap up to now
  (uncapped — see the invariant note above). The agent's active
  intervals also feed the session union — which is why the `sum:` and
  `main:` rows show IDENTICAL triples. While an agent's question hangs
  the session's own triple keeps counting the blocked time as WORK
  (waiting on agents is work by the union rule), even though the agent's
  personal row accrues it as wait.

Format: `HH:MM` via `format_duration` — seconds are dropped; hours are
unbounded (`03:45`, `103:25`); truncation never rounds a minute up.

Degradation: an event without a parsable timestamp is silently skipped
for timing; a session or agent with NO usable stamps (or a legacy direct
call bypassing the orchestrator) renders EMPTY time cells — never
`00:00`. Missing data means unknown, not zero. Transient clock-skew
protection: work is clamped with `min(work, total)` so the invariant
`work + wait = total` survives resumed/multi-dir sessions where an
agent's stamps start before main's first timestamp.

## Install

This module lives at `~/.claude/status_line/`. Claude Code invokes it
via the wrapper `status_line.sh`, which `exec`s `python3 status_line.py`.
No additional setup is required — the wrapper is referenced from Claude
Code's status-line hook configuration.

## Runtime dependencies

- **Python 3.9+** (only stdlib used: `collections.abc`, `datetime`,
  `json`, `math`, `os`, `re`, `subprocess`, `sys`, `time`,
  `urllib.parse`, `pathlib`, `typing`)
- **`git`** on `$PATH` (optional; absence is silently handled by
  returning `branch=""`)
- **`ANTHROPIC_BASE_URL`** env var (optional): the hook inherits it from
  the Claude Code process and uses its hostname for `model@host` price
  lookups (see [Costs](#costs-pricesjson)). Unset, invalid or scheme-less
  (no `://`) → no host → only plain model keys match.
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
6. Computes the time columns: with `now = time.time()` the orchestrator
   unions main turns + agent lifetimes into the session work/wait/total
   triple and injects each agent's personal durations as TRANSIENT keys —
   after the agents-cache write, so they never persist (see
   [Time columns](#time-columns-work--wait--total)). Tests freeze the
   clock by calling `_main_unsafe(now=…)` directly.
7. Renders the multi-line output (`render_output`), wiring in
   `prices.json`, the `ANTHROPIC_BASE_URL` hostname when a prices file
   exists (see [Costs](#costs-pricesjson)) and the time data.
8. Prints to stdout. Never returns non-zero.

### Caching

Two cache files are persisted under `~/.claude/status_line/data/`:

| File                | Invalidation key                       | Purpose                                                                                                                                             |
| ------------------- | -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `main_<sid>.json`   | `(last_uuid, mtime_jsonl)`             | per-model breakdown + first-message `start_*` + context occupancy + tool_use ids + time segmentation (`time_first_ts` / `time_turns` / `time_open`) |
| `agents_<sid>.json` | `(last_uuid, mtime_jsonl, mtime_meta)` | per-agent render-ready snapshot dict incl. time stamps (`ts_first` / `ts_last` / `qa_pauses` / `qa_open_ts`)                                        |

Both main-cache field groups added after the first release
(`context_tokens`, `start_in`/`start_out`/`start_cached`, `per_model`,
and the time-segmentation trio `time_first_ts`/`time_turns`/`time_open`)
are part of the cache-hit check: a pre-upgrade cache file that matches
the key but lacks them is treated as a miss and rescanned once, then
rewritten in the new shape.

Each per-agent entry in `agents_<sid>.json` is keyed by `agentId` and
holds the fields `last_uuid`, `mtime_jsonl`, `mtime_meta`, `status`,
`status_rev`, `tokens_in`, `tokens_out`, `tokens_cached`, `models`,
`description`, `toolUseId`, plus the four time-stamp fields `ts_first`,
`ts_last`, `qa_pauses`, `qa_open_ts`. The three `tokens_*` fields are the
CUMULATIVE breakdown columns rendered in the status line (input / output /
cache-read, summed over all of the agent's assistant events); `models`
is the per-model breakdown feeding the `model`/`cost` columns; the four
time fields persist so cache-HIT cycles can still apply live-now
extensions and AskUserQuestion wait splits. Cache-hit requires ALL of
these breakdown + time fields to be present AND the entry's `status_rev`
to equal the code's current status-logic revision — a pre-upgrade cache
missing any of them invalidates and triggers a forward re-parse (see
Edge cases). The derived durations (`time_work` / `time_wait` /
`time_total`) are deliberately NOT here: they are recomputed and
injected into the agent dicts after every cache write by the
orchestrator.

`status_rev` deserves its own word: a dead agent's jsonl never mutates
again, so the `(last_uuid, mtime)` key would keep hitting a cache entry
forever — including a `status` value classified by an older, buggier
`detect_status`. The rev stamp (bumped whenever status classification
changes) turns such entries into misses: they are rescanned once and
rewritten with the corrected status.

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
  requires `tokens_in`/`tokens_out`/`tokens_cached`/`models` plus the
  four time fields (`ts_first`/`ts_last`/`qa_pauses`/`qa_open_ts`) to be
  present, and the entry's `status_rev` to match the current code; if
  any fail, the entry is treated as a miss and the jsonl is re-scanned.
  After the first such re-scan the cache is rewritten with the new shape
  and subsequent calls hit cleanly. Without this check, a stale entry
  would render zeros (via `int(field or 0)`), blank duration cells — or,
  for a pre-rev `status`, a wrong `[run]`/`[err]` tag — until the next
  jsonl mutation (which, for a dead agent, never comes).
- **Missing / unparsable timestamps**: events without an ISO 8601 stamp
  are silently skipped for timing; a session or agent with no usable
  stamps at all renders EMPTY work/wait/total cells (never `00:00`).
  JSON `null` in cached time fields passes the presence hit-guard but
  coerces to "no data" by the repo's usual defensive-read convention
  (`_to_float`, which also rejects booleans and non-finite junk — a bare
  `NaN`/`Infinity` a hand-edited cache parses to would otherwise poison
  the arithmetic into degrading the whole output) — the affected cells
  render empty rather than crashing the arithmetic.
- **Legacy direct call** (`render_output` without `main_time`): rows keep
  their structure, every duration cell stays empty — only the header
  labels appear. `_main_unsafe` itself takes a REQUIRED `now` — there is
  no blank-time orchestrator mode that could silently hide a wiring
  mistake.
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

499 tests cover: pure functions (`format_tokens`, `format_duration`,
`union_work`, `_parse_ts` in `tests/test_format_duration.py` /
`tests/test_union_work.py`, the agent pause/trim/extension geometry in
`tests/test_agent_time_segments.py`, `detect_status`, `parse_stdin`),
price helpers (`provider_host`, `load_prices`, `price_for`,
`compute_cost`, `format_cost`), I/O helpers (`compute_main_cum`,
`compute_agent_snapshot`, `find_session_dir(s)`, `_resolve_session_dirs`,
`sort_agents`, `_write_agents_cache`) including the main/agent time
segmentation and cache presence guards (`tests/test_time_segmentation.py`),
`render_table` and `render_output` (model/cost columns, per-model groups,
the `start:` row, the always-visible work/wait/total block),
`main()` end-to-end against a real session fixture — including the
multi-dir merge across duplicate session dirs
(`tests/test_resolve_session_dirs.py`, `tests/test_find_session_dir.py`)
and the work/wait/total invariants (`work + wait == total` within ±1s,
identical `main:` / `sum:` triples) checked in a now-independent way — a
frozen-clock suite calling `_main_unsafe(now=…)` in-process that also
pins the live-now extensions (open main turn, run agent), the hanging-QA
agent triple, the `min(work, total)` and clock-skew clamps, the
null-time-fields cache-hit degrade, and stray bare-NaN/Infinity transient
time fields in a hand-corrupted agents cache (blank cells, never a crash)
— a runtime smoke test — and the bash wrapper.

### Real-session fixture

`tests/fixtures/real_session/` contains a copy of session
`f5044e4f-3e01-4330-be72-eb008a1d035e` (38 subagents) used by
`test_main_integration.py`. The directory is gitignored — populate it
after a fresh clone (see `tests/fixtures/real_session/README.md`).
