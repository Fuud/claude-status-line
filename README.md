# status_line

Real-time Claude Code status line that aggregates tokens across the main
session and all subagents. Replaces the previous bash+jq implementation
that only tracked main-session tokens.

## What it does

Renders a multi-line status string consumed by the Claude Code status-line
hook. Layout:

```
Session: <sid> | Branch: <git-branch> | Model: <model> | User: n/a
sum: 53.2M
main: 50.8M
[ok]    Review: implementation    103k
[err]   Review: quality
[run]   Task 4: MissingGlyphLog
```

Each agent line carries one of four status tags:

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
2. Resolves `session_id` → `~/.claude/projects/<encoded>/<sid>/` via
   `find_session_dir`.
3. Computes cumulative tokens for the main jsonl (`compute_main_cum`),
   with on-disk cache at `~/.claude/status_line/data/main_<sid>.json`.
4. Iterates subagent jsonl files, computing a per-agent snapshot
   (`compute_agent_snapshot`) using the agents cache at
   `~/.claude/status_line/data/agents_<sid>.json`.
5. Sorts agents by main-jsonl `tool_use` position (`sort_agents`).
6. Renders the multi-line output (`render_output`).
7. Prints to stdout. Never returns non-zero.

### Caching

Two cache files are persisted under `~/.claude/status_line/data/`:

| File                | Invalidation key                       | Purpose                                |
| ------------------- | -------------------------------------- | -------------------------------------- |
| `main_<sid>.json`   | `last_uuid` (tail of main jsonl)       | cumulative token totals + tool_use ids |
| `agents_<sid>.json` | `(last_uuid, mtime_jsonl, mtime_meta)` | per-agent render-ready snapshot dict   |

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
  (or `[stop]` if `meta.stoppedByUser=true`).

## Tests

Run all tests:

```bash
cd ~/.claude/status_line
python3 -m pytest tests/ -v
```

74+ tests cover: pure functions (`format_tokens`, `detect_status`,
`parse_stdin`), I/O helpers (`compute_main_cum`, `compute_agent_snapshot`,
`find_session_dir`, `sort_agents`), `render_output`, `main()` end-to-end
against a real session fixture, and the bash wrapper.

### Real-session fixture

`tests/fixtures/real_session/` contains a copy of session
`f5044e4f-3e01-4330-be72-eb008a1d035e` (38 subagents) used by
`test_main_integration.py`. The directory is gitignored — populate it
after a fresh clone (see `tests/fixtures/real_session/README.md`).
