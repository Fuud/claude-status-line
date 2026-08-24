# tests/fixtures/real_session/

The real_session fixture is a copy of a real Claude Code session
(`f5044e4f-3e01-4330-be72-eb008a1d035e`) used by the integration tests in
`tests/test_main_integration.py`. The directory contains:

```
f5044e4f-3e01-4330-be72-eb008a1d035e/   # session dir, has subagents/
f5044e4f-3e01-4330-be72-eb008a1d035e.jsonl  # main jsonl, sibling of session dir
```

## Why this directory is gitignored

The fixture is ~14 MB (38 subagent jsonl files + 1.7 MB main jsonl). It's
gitignored at the repo root (`tests/fixtures/real_session/`) because
shipping it in git history would bloat every clone for a test fixture
that's only meaningful to one specific session snapshot.

## Setting up the fixture (fresh clone)

The integration tests require this directory to exist. After cloning the
repo, populate it by copying the current real session from your local
Claude Code projects dir:

```bash
# pick the source session id — adjust if you're targeting a different session
SID="f5044e4f-3e01-4330-be72-eb008a1d035e"
SRC="$HOME/.claude/projects/C--Users-f-bobin-IdeaProjects-agentic-terminal"

DST="tests/fixtures/real_session"
mkdir -p "$DST"
cp -r "$SRC/$SID" "$DST/"
cp    "$SRC/$SID.jsonl" "$DST/"
```

The integration tests detect the fixture via
`tests/fixtures/real_session/<sid>/` and `<sid>.jsonl`. If the fixture
is missing, `test_main_integration.py` will fail its initial
`assert real_session_root.exists()` check.

## Regenerating the fixture

When the source session mutates (subagents added, sessions extended,
statuses changed), the fixture must be re-copied. Test assertions
keyed to specific content — e.g.
`test_real_session_38_agents` asserting that the first agent line is
"Review implementation plan" — will break otherwise. Re-run the
`cp -r` snippet above, then run `pytest tests/test_main_integration.py -v`
to confirm all integration tests still pass.

[deviation] The original fixture was copied from a session snapshot
where the assertion `assert "Review implementation plan" in first_agent`
holds. If you regenerate against a session that has different agents or
order, update the assertions in `test_main_integration.py` accordingly.
