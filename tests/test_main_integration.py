"""Integration tests for main() — end-to-end runs with subprocesses.

Each test spawns `status_line.py` as a subprocess (via `sys.executable`)
and feeds it JSON on stdin, then asserts on the produced stdout. Tests
that need a real session dir monkeypatch `HOME` for the subprocess so
that `Path.home()` resolves to a tmp_path containing a symlink to
`tests/fixtures/real_session/<sid>/`.

Layout under fake HOME:
    <tmp>/.claude/projects/<encoded>/<sid>/  (symlinked to fixture)
    <tmp>/.claude/status_line/data/          (cache writes)
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import status_line
from status_line import _parse_ts

FIXTURES = Path(__file__).parent / "fixtures"
REAL_SESSION_SID = "f5044e4f-3e01-4330-be72-eb008a1d035e"
ENCODED_PROJECT = "C--Users-f-bobin-IdeaProjects-agentic-terminal"

STATUS_LINE_PY = Path(__file__).parent.parent / "status_line.py"


def _run_main(
    stdin: str, home: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Spawn status_line.py with HOME=<home> and feed stdin (str).

    CLAUDE_CODE_CONTEXT_LIMIT and ANTHROPIC_BASE_URL are always POPPED so
    context-limit resolution and provider-host matching are deterministic
    (the machine's own values must not leak into the subprocess); pass
    per-test overrides via extra_env to set them again."""
    env = os.environ.copy()
    # Path.home() on Windows Python consults USERPROFILE first, then HOME.
    # Override both so the child process sees a deterministic home.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    env.pop("CLAUDE_CODE_CONTEXT_LIMIT", None)
    # provider_host() reads this in the child; without the pop the machine
    # env decides whether "@host"-keyed prices match, making those tests
    # machine-dependent.
    env.pop("ANTHROPIC_BASE_URL", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(STATUS_LINE_PY)],
        input=stdin.encode("utf-8"),
        capture_output=True,
        timeout=30,
        env=env,
    )


# ---------------------------------------------------------------------------
# Time-cell plumbing shared by the Task 6 tests (plan
# 20260827-status-line-time-columns). With the orchestrator wired
# (main() passes now=time.time()) every session/agent row ends with three
# live "HH:MM:SS" cells whose VALUES depend on wall-clock time. Tests
# therefore either pin exact values (the frozen-now in-process runs in
# section 13) or mask durations away (legacy cross-invocation equality).
# ---------------------------------------------------------------------------

_DUR_CELL = r"\d{1,}:[0-5]\d:[0-5]\d"
_DUR_CELL_RE = re.compile(_DUR_CELL)


def _mask_durations(text: str) -> str:
    """Replace every HH:MM:SS duration cell with "<DUR>".

    Consecutive hook invocations over unchanged files differ ONLY in the
    elapsed-time digits (live-now durations grow), so masking them makes
    byte-level output comparisons meaningful again."""
    return _DUR_CELL_RE.sub("<DUR>", text)


def _hms_seconds(cell: str) -> int:
    """Parse "HH:MM:SS" into total seconds. Raises on anything else so a
    shifted/missing time column fails loudly instead of silently passing."""
    hours, minutes, secs = cell.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + int(secs)


def _is_duration_cell(cell: str) -> bool:
    return re.fullmatch(_DUR_CELL, cell) is not None


@pytest.fixture
def fake_home_with_real_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a fake $HOME with .claude/projects/<encoded>/<sid> pointing
    at the real_session fixture, and a writable .claude/status_line/data/.

    Skips (rather than fails) when the gitignored fixture is not
    populated — same convention as
    test_real_session_fixture_has_no_subagent_queue_notifications below.
    Hermetic coverage of the exact per-model/cost arithmetic lives in
    test_synth_prices_per_model_rows_and_costs, which never depends on
    the fixture.

    Returns (tmp_path, session_id).
    """
    real_session_root = FIXTURES / "real_session"
    real_session_dir = real_session_root / REAL_SESSION_SID
    if not real_session_root.exists() or not real_session_dir.exists():
        pytest.skip(
            "real_session fixture not populated; see "
            "tests/fixtures/real_session/README.md"
        )

    projects_root = tmp_path / ".claude" / "projects"
    target = projects_root / ENCODED_PROJECT / REAL_SESSION_SID
    target.parent.mkdir(parents=True)
    # Symlink the session dir (which contains subagents/) and copy the main
    # jsonl file alongside it. Symlinks are read-only proxies; shutil.copy
    # keeps the jsonl file local so cache writes against data/ stay isolated.
    if target.is_symlink() or target.exists():
        if target.is_symlink():
            target.unlink()
        else:
            shutil.rmtree(target)
    target.symlink_to(real_session_dir.resolve(), target_is_directory=True)

    main_jsonl_src = real_session_root / f"{REAL_SESSION_SID}.jsonl"
    main_jsonl_dst = target.parent / f"{REAL_SESSION_SID}.jsonl"
    shutil.copy(main_jsonl_src, main_jsonl_dst)

    # Writable data dir for cache files
    data_dir = tmp_path / ".claude" / "status_line" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    return tmp_path, REAL_SESSION_SID


# ---------------------------------------------------------------------------
# 1. Real session → 43 lines (header + table header + start + sum + main + 38
#    agents)
# ---------------------------------------------------------------------------

def test_real_session_38_agents(fake_home_with_real_session) -> None:
    """Feed the real session through main(); expect 43 lines, presence of
    [ok]/[err] tags, and 'Review implementation plan' as the first agent
    line (lowest toolUseId position in main jsonl).

    [deviation] The f5044e4f session evolved after the plan was written —
    when the fixture was copied the session had no agents with
    stoppedByUser=true in meta, so [stop] is not currently present in
    the snapshot. We only assert [ok] and [err] here. If a future session
    snapshot has stopped agents, add the [stop] assertion back.
    """
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "MiniMax-M3"},
        "context_window": {"used_percentage": 0, "total_input_tokens": 0},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    output = result.stdout.decode("utf-8")
    lines = output.splitlines()
    # header + table header + start + sum + main + 38 agents = 43
    assert len(lines) == 43, (
        f"expected 43 lines, got {len(lines)}; first 5: {lines[:5]}; "
        f"stderr: {result.stderr.decode('utf-8', 'replace')}"
    )
    # All three status tags must appear (real session covers ok/err/stop).
    assert "[ok]" in output, "expected at least one [ok] in output"
    assert "[err]" in output, "expected at least one [err] in output"
    # Header / table header / start / sum / main lines have predictable prefixes
    assert lines[0].startswith("Session:"), f"line 0: {lines[0]!r}"
    # Table header line (the breakdown-table labels): contains all three labels
    # "in" / "out" / "cached", each right-aligned under its own column.
    assert "in" in lines[1] and "out" in lines[1] and "cached" in lines[1], (
        f"line 1 (table header) should contain in/out/cached labels: {lines[1]!r}"
    )
    assert not lines[1].startswith("sum:"), (
        f"line 1 must be the table header, not the sum line: {lines[1]!r}"
    )
    assert lines[2].startswith("| start:"), f"line 2: {lines[2]!r}"
    assert lines[3].startswith("| sum:"), f"line 3: {lines[3]!r}"
    assert lines[4].startswith("| main:"), f"line 4: {lines[4]!r}"
    # All agent lines start with the table prefix + a bracketed status tag
    for line in lines[5:]:
        assert line.startswith("| ["), f"agent line missing status tag: {line!r}"
    # The first agent in the output should be the one with the LOWEST
    # tool_use position in main jsonl. In the f5044e4f session that's
    # "Review implementation plan" (toolUseId=Agent_61, position 260 in
    # the main jsonl) — not Task 1 (Agent_103, position 431). The plan's
    # assertion "Task 1 first" assumed Task 1 was at position 0; the real
    # session has Agent_61/62/... before Agent_103. We check for the actual
    # first-sorted agent instead.
    first_agent = lines[5]
    assert "Review implementation plan" in first_agent, (
        f"first agent should be 'Review implementation plan' (lowest toolUseId "
        f"in main jsonl), got: {first_agent!r}"
    )
    # And Task 1 should appear SOMEWHERE in the output (just not first).
    assert any("Task 1" in line for line in lines), (
        "Task 1 should appear in the output even if not first"
    )


# ---------------------------------------------------------------------------
# 2. Empty session_id → header only
# ---------------------------------------------------------------------------

def test_empty_session_id_only_header(fake_home_with_real_session) -> None:
    """Empty session_id → stdout is exactly 1 line (header), exit 0."""
    tmp_path, _ = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": "",
        "model": {"display_name": "X"},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0
    output = result.stdout.decode("utf-8")
    lines = output.splitlines()
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {output!r}"
    assert lines[0].startswith("Session:"), f"line 0: {lines[0]!r}"
    # ensure nothing after the header line — output ends right after \n
    assert output.endswith("\n")
    assert output.count("\n") == 1


# ---------------------------------------------------------------------------
# 3. Nonexistent session_id → header only
# ---------------------------------------------------------------------------

def test_nonexistent_session_id_only_header(fake_home_with_real_session) -> None:
    """Valid-format but non-existent session_id → header only, exit 0."""
    tmp_path, _ = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": "00000000-0000-0000-0000-000000000000",
        "model": {"display_name": "X"},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0
    output = result.stdout.decode("utf-8")
    lines = output.splitlines()
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {output!r}"
    assert lines[0].startswith("Session:")


# ---------------------------------------------------------------------------
# 4. Status tag counts (real session has all three: ok/err/stop)
# ---------------------------------------------------------------------------

def test_status_tag_counts(fake_home_with_real_session) -> None:
    """Real session has [ok] and [err] agents at minimum.

    [deviation] The fixture for f5044e4f no longer contains any agents
    with stoppedByUser=true in meta or [Request interrupted by user] in
    their last event, so no [stop] tags appear. The plan claimed 2
    stopped agents at the time of writing; the session has since been
    extended and re-run, mutating those agents into run/ok. We assert
    what the fixture actually contains — at least one [err] and at
    least one [ok], without pinning exact counts so the test survives
    future fixture regenerations.
    """
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "X"},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0
    output = result.stdout.decode("utf-8")
    assert output.count("[err]") >= 1, (
        f"expected at least one [err] agent, output:\n{output}"
    )
    assert output.count("[ok]") >= 1, (
        f"expected at least one [ok] agent, output:\n{output}"
    )


# ---------------------------------------------------------------------------
# 5. Broken cache recovery
# ---------------------------------------------------------------------------

def test_broken_cache_recovery(fake_home_with_real_session) -> None:
    """Pre-write garbage to data/main_<sid>.json; main() must recover,
    exit 0, and leave a parseable cache file behind."""
    tmp_path, sid = fake_home_with_real_session
    data_dir = tmp_path / ".claude" / "status_line" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir / f"main_{sid}.json"
    cache_path.write_text("this is not valid json {{{", encoding="utf-8")
    assert cache_path.exists()

    stdin = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    # Cache must now be valid JSON (recomputed by compute_main_cum after
    # detecting JSONDecodeError and deleting the bad file).
    assert cache_path.exists(), "cache file should be rewritten"
    loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    # The recomputed cache should match real session's main jsonl signature.
    assert "last_uuid" in loaded
    # [deviation] Task 2 dropped the `total` field, and the model-columns
    # refactor dropped the flat cum_* sums — the per-model breakdown
    # carries the same information. Verify the live keys exist and reflect
    # real usage.
    assert "per_model" in loaded
    assert loaded["per_model"], "per_model should be non-empty for the fixture"
    assert "total" not in loaded, (
        f"`total` must not appear in the persisted cache after Task 2, "
        f"got keys: {sorted(loaded.keys())}"
    )
    assert not any(k.startswith("cum_") for k in loaded), (
        f"removed cum_* keys must not be persisted, got: {sorted(loaded.keys())}"
    )
    assert loaded["context_tokens"] > 0


def test_broken_agents_cache_recovery(fake_home_with_real_session) -> None:
    """Pre-write garbage to data/agents_<sid>.json; main() must recover,
    exit 0, and leave a parseable agents cache file behind.

    This mirrors test_broken_cache_recovery but exercises the per-agent
    cache path, which has a separate load+validate block in main().
    """
    tmp_path, sid = fake_home_with_real_session
    data_dir = tmp_path / ".claude" / "status_line" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    agents_cache_path = data_dir / f"agents_{sid}.json"
    agents_cache_path.write_text("not json {", encoding="utf-8")
    assert agents_cache_path.exists()

    stdin = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    # The agents cache must now be valid JSON (main() falls through to
    # writing a fresh cache after detecting JSONDecodeError).
    assert agents_cache_path.exists(), "agents cache should be rewritten"
    loaded = json.loads(agents_cache_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    # Should have one entry per subagent jsonl file (38 in the fixture).
    assert len(loaded) >= 1
    # Spot-check: each entry has last_uuid + mtime_jsonl + status.
    sample_key = next(iter(loaded))
    sample = loaded[sample_key]
    assert "last_uuid" in sample
    assert "mtime_jsonl" in sample
    assert "status" in sample


def test_agents_cache_non_dict_recovery(fake_home_with_real_session) -> None:
    """Pre-write a JSON LIST (valid JSON, not a dict) to agents_<sid>.json;
    main() must detect non-dict payload, delete the bad cache, and rebuild."""
    tmp_path, sid = fake_home_with_real_session
    data_dir = tmp_path / ".claude" / "status_line" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    agents_cache_path = data_dir / f"agents_{sid}.json"
    agents_cache_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    stdin = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0
    loaded = json.loads(agents_cache_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    assert len(loaded) >= 1


# ---------------------------------------------------------------------------
# 6. Empty stdin (no JSON at all) → header only with empty session_id
# ---------------------------------------------------------------------------

def test_empty_stdin_only_header(fake_home_with_real_session) -> None:
    """No stdin content at all → header only with empty session_id, exit 0.

    This is the failure-mode that protects the user: if the hook gets
    no payload, status line must not crash the parent process.
    """
    tmp_path, _ = fake_home_with_real_session
    result = _run_main("", tmp_path)
    assert result.returncode == 0
    output = result.stdout.decode("utf-8")
    lines = output.splitlines()
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {output!r}"
    assert lines[0].startswith("Session:")
    # Session_id slot in header should be empty
    assert "Session:  |" in lines[0], f"expected empty sid in header: {lines[0]!r}"


# ---------------------------------------------------------------------------
# 7. Re-run main() with a valid agents cache on disk
# ---------------------------------------------------------------------------

def test_second_call_after_cache(fake_home_with_real_session) -> None:
    """Run main() twice with the same stdin.

    The first invocation populates the agents_<sid>.json cache; the second
    invocation hits the cache for every subagent and exercises the
    compute_agent_snapshot cache-hit early-return path. Without the
    agentId-reinjection fix, the second call raised KeyError inside
    _write_agents_cache and main()'s except clause silently degraded to
    the hardcoded fallback header ("Session:  | Branch:  | Model:  | User:
    n/a") — a single line instead of the expected 42.

    This is the actual runtime scenario: the status-line hook fires every
    few seconds, so every real invocation is a "second call after cache"
    case.
    """
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "MiniMax-M3"},
        "context_window": {"used_percentage": 0, "total_input_tokens": 0},
    })
    first = _run_main(stdin, tmp_path)
    assert first.returncode == 0, (
        f"first call failed; stderr={first.stderr.decode('utf-8', 'replace')}"
    )
    first_output = first.stdout.decode("utf-8")
    first_lines = first_output.splitlines()
    assert len(first_lines) == 43, (
        f"first call should produce 43 lines (header + table header + start + "
        f"sum + main + 38 agents), got {len(first_lines)}; first 3: {first_lines[:3]}"
    )

    # Sanity: the agents cache file must exist after the first call.
    data_dir = tmp_path / ".claude" / "status_line" / "data"
    agents_cache_path = data_dir / f"agents_{sid}.json"
    assert agents_cache_path.exists(), "agents cache should be written by 1st call"

    # 2nd call — exercises the cache-hit path for every subagent. Without
    # the fix, this returns 1 line (the hardcoded fallback header).
    second = _run_main(stdin, tmp_path)
    assert second.returncode == 0, (
        f"second call failed; stderr={second.stderr.decode('utf-8', 'replace')}"
    )
    second_output = second.stdout.decode("utf-8")
    second_lines = second_output.splitlines()
    assert len(second_lines) == 43, (
        f"second call should also produce 43 lines (cache-hit path), "
        f"got {len(second_lines)}; first 3: {second_lines[:3]}; "
        f"this indicates compute_agent_snapshot cache-hit is missing agentId "
        f"and _write_agents_cache raised KeyError, which main() swallowed "
        f"into the fallback header"
    )
    # Header must reflect the real session_id, not the empty fallback.
    assert "Session: " + sid in second_lines[0], (
        f"second call header should contain the real session_id, got: "
        f"{second_lines[0]!r}"
    )
    # Output must be identical to the first call apart from the live-now
    # duration cells (the cache hit should be deterministic for unchanged
    # files; since plan 20260827-status-line-time-columns main() passes
    # now=time.time(), the work/wait/total cells legitimately grow between
    # invocations — mask them before comparing).
    assert _mask_durations(second_output) == _mask_durations(first_output), (
        "cache-hit output diverged from cache-miss output:\n"
        f"first:  {first_output[:200]!r}\n"
        f"second: {second_output[:200]!r}"
    )
    # Both calls rendered REAL durations on the sum row (time columns are
    # populated end-to-end, not blank).
    sum_cells = first_lines[3].split()
    assert all(_is_duration_cell(c) for c in sum_cells[-3:]), (
        f"sum row lacks HH:MM:SS work/wait/total cells: {lines[3]!r}"
    )


# ---------------------------------------------------------------------------
# 8. Subagent status via main-log task-notifications (added per
#    20260824-subagent-status-via-queue-notifications). These tests build a
#    SYNTHETIC session in tmp_path and run main() against it — they do NOT
#    depend on the gitignored real_session fixture.
# ---------------------------------------------------------------------------

SYNTH_SID = "11111111-2222-3333-4444-555555555555"


def _build_synth_session(
    tmp_path: Path,
    sid: str,
    main_jsonl_lines: list[str],
    agent_files: list[tuple[str, str, str]],
    encoded: str = "synthetic-project",
) -> None:
    """Populate a synthetic session under tmp_path/.claude/projects/<encoded>/.

    Args:
        sid: session id used for the directory name and main jsonl filename.
        main_jsonl_lines: list of JSON lines for the main jsonl.
        agent_files: list of (agent_id, jsonl_content, meta_content) tuples;
            each is written into session_dir/subagents/.
        encoded: encoded project directory name. Defaults to the historical
            "synthetic-project"; pass a distinct name to build a SECOND
            project dir for the same sid (duplicate-session-dir layout —
            see section 11 tests).
    """
    session_dir = (tmp_path / ".claude" / "projects" / encoded / sid)
    subagents = session_dir / "subagents"
    subagents.mkdir(parents=True)
    main_jsonl = session_dir.parent / f"{sid}.jsonl"
    main_jsonl.write_text("\n".join(main_jsonl_lines) + "\n")
    for agent_id, jsonl_content, meta_content in agent_files:
        (subagents / f"{agent_id}.jsonl").write_text(jsonl_content)
        (subagents / f"{agent_id}.meta.json").write_text(meta_content)


def test_real_session_fixture_has_no_subagent_queue_notifications() -> None:
    """Inspection guard: if the real_session fixture is present, assert that
    its main jsonl has NO queue-operation events whose <task-id> matches any
    agent-* filename stem. This is the precondition for existing assertions
    (e.g. exact `[err]` count in test_status_tag_counts) to remain valid —
    if the fixture ever grows subagent task-notifications, those tests
    would silently start producing [kill] tags.

    The test is a no-op (skip) when the fixture is absent — gitignored."""
    real_session_root = FIXTURES / "real_session"
    if not real_session_root.exists():
        pytest.skip("real_session fixture not populated; see fixtures/real_session/README.md")

    main_jsonl_path = real_session_root / f"{REAL_SESSION_SID}.jsonl"
    if not main_jsonl_path.exists():
        pytest.skip("real_session main jsonl missing")

    # Collect all agent-* stems (minus "agent-" prefix)
    subagents_dir = real_session_root / REAL_SESSION_SID / "subagents"
    agent_stems: set[str] = set()
    if subagents_dir.exists():
        for p in subagents_dir.glob("agent-*.jsonl"):
            agent_stems.add(p.stem.removeprefix("agent-"))

    assert agent_stems, "no subagent fixtures found"

    # Scan main jsonl for queue-operation events with <task-id> matching an
    # agent stem. There should be zero — the existing fixture has only
    # background-bash task-notifications.
    matching_count = 0
    for raw_line in main_jsonl_path.read_text().splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "queue-operation":
            continue
        if event.get("operation") != "enqueue":
            continue
        content = event.get("content")
        if not isinstance(content, str):
            continue
        # cheap substring check before regex
        if "<task-id>" not in content:
            continue
        m = re.search(r"<task-id>([^<]+)</task-id>", content)
        if m and m.group(1) in agent_stems:
            matching_count += 1

    assert matching_count == 0, (
        f"real_session fixture has {matching_count} subagent queue-events — "
        "would silently change [err]/[stop]/[kill] counts in "
        "test_status_tag_counts. Regenerate fixture or update assertions."
    )


def test_synth_killed_in_tool_use_renders_as_kill(tmp_path: Path) -> None:
    """End-to-end: agent jsonl ends with tool_use (no end_turn); main jsonl
    has queue-operation <status>killed</status> with task-id matching the
    agent stem. Output must contain [kill] tag for that agent."""
    agent_id = "agent-aaa111"
    task_id = agent_id.removeprefix("agent-")
    main_lines = [
        '{"type":"assistant","message":{"role":"assistant","content":[],"model":"x","stop_reason":"end_turn","usage":{}},"uuid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","sessionId":"x","timestamp":"2026-08-24T20:00:00.000Z"}',
        f'{{"type":"queue-operation","operation":"enqueue","timestamp":"2026-08-24T20:00:01.000Z","sessionId":"x","content":"<task-notification>\\n<task-id>{task_id}</task-id>\\n<status>killed</status>\\n</task-notification>"}}',
    ]
    agent_jsonl = (
        '{"type":"user","message":{"role":"user","content":"x"},"uuid":"u1","sessionId":"x","timestamp":"2026-08-24T20:00:00.000Z"}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{}}],"model":"x","stop_reason":"tool_use","usage":{"input_tokens":10,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":5}},"uuid":"a1","sessionId":"x","timestamp":"2026-08-24T20:00:00.500Z"}'
    )
    agent_meta = json.dumps({
        "agentType": "general-purpose",
        "description": "Task 1: synth",
        "toolUseId": "t1",
        "spawnDepth": 1,
    })
    _build_synth_session(
        tmp_path, SYNTH_SID, main_lines, [(agent_id, agent_jsonl, agent_meta)]
    )

    stdin = json.dumps({"session_id": SYNTH_SID, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    output = result.stdout.decode("utf-8")
    # The agent line must carry [kill], NOT [run].
    assert "[kill]" in output, f"expected [kill] in output, got:\n{output!r}"
    assert "[run]" not in output, f"agent should NOT show [run]; got:\n{output!r}"
    assert "Task 1: synth" in output


def test_synth_completed_after_tool_use_renders_as_ok(tmp_path: Path) -> None:
    """End-to-end: agent jsonl ends with end_turn; queue says completed.
    Output renders [ok]."""
    agent_id = "agent-bbb222"
    task_id = agent_id.removeprefix("agent-")
    main_lines = [
        '{"type":"assistant","message":{"role":"assistant","content":[],"model":"x","stop_reason":"end_turn","usage":{}},"uuid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","sessionId":"x","timestamp":"2026-08-24T20:01:00.000Z"}',
        f'{{"type":"queue-operation","operation":"enqueue","timestamp":"2026-08-24T20:01:01.000Z","sessionId":"x","content":"<task-notification>\\n<task-id>{task_id}</task-id>\\n<status>completed</status>\\n</task-notification>"}}',
    ]
    agent_jsonl = (
        '{"type":"user","message":{"role":"user","content":"x"},"uuid":"u1","sessionId":"x","timestamp":"2026-08-24T20:01:00.000Z"}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"model":"x","stop_reason":"end_turn","usage":{"input_tokens":10,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":5}},"uuid":"a1","sessionId":"x","timestamp":"2026-08-24T20:01:00.500Z"}'
    )
    agent_meta = json.dumps({
        "agentType": "general-purpose",
        "description": "Task 2: review",
        "toolUseId": "t2",
        "spawnDepth": 1,
    })
    _build_synth_session(
        tmp_path, SYNTH_SID, main_lines, [(agent_id, agent_jsonl, agent_meta)]
    )

    stdin = json.dumps({"session_id": SYNTH_SID, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    output = result.stdout.decode("utf-8")
    assert "[ok]" in output, f"expected [ok] in output, got:\n{output!r}"
    assert "Task 2: review" in output


def test_synth_no_queue_event_behaves_as_before(tmp_path: Path) -> None:
    """Regression: agent without queue-event (or no matching <task-id>) →
    falls through to jsonl-based detection. agent jsonl ends with tool_use
    (mid-flight) → [run]."""
    agent_id = "agent-ccc333"
    # main_jsonl has NO queue-operation events at all
    main_lines = [
        '{"type":"assistant","message":{"role":"assistant","content":[],"model":"x","stop_reason":"end_turn","usage":{}},"uuid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","sessionId":"x","timestamp":"2026-08-24T20:02:00.000Z"}',
    ]
    agent_jsonl = (
        '{"type":"user","message":{"role":"user","content":"x"},"uuid":"u1","sessionId":"x","timestamp":"2026-08-24T20:02:00.000Z"}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t3","name":"Bash","input":{}}],"model":"x","stop_reason":"tool_use","usage":{"input_tokens":10,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":5}},"uuid":"a1","sessionId":"x","timestamp":"2026-08-24T20:02:00.500Z"}'
    )
    agent_meta = json.dumps({
        "agentType": "general-purpose",
        "description": "Task 3: in-flight",
        "toolUseId": "t3",
        "spawnDepth": 1,
    })
    _build_synth_session(
        tmp_path, SYNTH_SID, main_lines, [(agent_id, agent_jsonl, agent_meta)]
    )

    stdin = json.dumps({"session_id": SYNTH_SID, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    output = result.stdout.decode("utf-8")
    # No queue-event → falls back to jsonl → tool_use as last event → run.
    assert "[run]" in output, f"expected [run] in output, got:\n{output!r}"
    assert "[kill]" not in output


def test_synth_queue_event_after_assistant_rerun_uses_cached_data(
    tmp_path: Path,
) -> None:
    """End-to-end cache behavior: first call populates main_<sid>.json with
    an empty task_notifications (queue event hasn't fired yet). Then we
    APPEND a queue event to main jsonl (which bumps mtime but NOT last_uuid
    — we keep the assistant event the same). The second call must:
      - see the new mtime, invalidate the cache
      - pick up the new queue-event → override agent status to [kill]
    """
    agent_id = "agent-ddd444"
    task_id = agent_id.removeprefix("agent-")
    main_lines_v1 = [
        '{"type":"assistant","message":{"role":"assistant","content":[],"model":"x","stop_reason":"end_turn","usage":{}},"uuid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","sessionId":"x","timestamp":"2026-08-24T20:03:00.000Z"}',
    ]
    agent_jsonl = (
        '{"type":"user","message":{"role":"user","content":"x"},"uuid":"u1","sessionId":"x","timestamp":"2026-08-24T20:03:00.000Z"}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t4","name":"Bash","input":{}}],"model":"x","stop_reason":"tool_use","usage":{"input_tokens":10,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":5}},"uuid":"a1","sessionId":"x","timestamp":"2026-08-24T20:03:00.500Z"}'
    )
    agent_meta = json.dumps({
        "agentType": "general-purpose",
        "description": "Task 4: mid-flight",
        "toolUseId": "t4",
        "spawnDepth": 1,
    })
    _build_synth_session(
        tmp_path, SYNTH_SID, main_lines_v1, [(agent_id, agent_jsonl, agent_meta)]
    )

    stdin = json.dumps({"session_id": SYNTH_SID, "model": {"display_name": "X"}})

    # 1st call — no queue-event yet → [run].
    r1 = _run_main(stdin, tmp_path)
    assert r1.returncode == 0, r1.stderr.decode("utf-8", "replace")
    out1 = r1.stdout.decode("utf-8")
    assert "[run]" in out1, f"1st call expected [run]; got:\n{out1!r}"

    # Append a queue-event with the new (post-completion) status.
    main_jsonl = (
        tmp_path / ".claude" / "projects" / "synthetic-project" / f"{SYNTH_SID}.jsonl"
    )
    queue_line = json.dumps({
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": "2026-08-24T20:03:01.000Z",
        "sessionId": "x",
        "content": (
            "<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            "<status>killed</status>\n"
            "</task-notification>"
        ),
    })
    with main_jsonl.open("a") as f:
        f.write(queue_line + "\n")

    # 2nd call — must detect the queue-event via mtime bump, override to [kill].
    r2 = _run_main(stdin, tmp_path)
    assert r2.returncode == 0, r2.stderr.decode("utf-8", "replace")
    out2 = r2.stdout.decode("utf-8")
    assert "[kill]" in out2, f"2nd call expected [kill]; got:\n{out2!r}"
    assert "[run]" not in out2, f"2nd call should NOT have [run]; got:\n{out2!r}"


# ---------------------------------------------------------------------------
# 9. Header "Context: NK (P%)" field (2026-08-24). Sources: payload
#    context_window.total_input_tokens first, jsonl last-assistant occupancy
#    as fallback; divisor: env CLAUDE_CODE_CONTEXT_LIMIT, else "[1m]" model
#    → 1M, else 200K.
# ---------------------------------------------------------------------------

# Payload context value used across these tests: 15500 tokens.
# Against 200K → "16K (8%)" (round(15.5)=16, round(7.75)=8).
# Against 1M   → "16K (2%)"  (round(1.55)=2).
# Against 500K → "16K (3%)"  (round(3.1)=3).
CTX_TOKENS = 15_500


def test_header_context_from_payload(fake_home_with_real_session) -> None:
    """Payload carries context_window.total_input_tokens → header shows it
    after User, percent vs the 200K default (plain model, no env)."""
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "X"},
        "context_window": {"used_percentage": 8, "total_input_tokens": CTX_TOKENS},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    header = result.stdout.decode("utf-8").splitlines()[0]
    assert "| Context: 16K (8%)" in header, f"header: {header!r}"
    # Segment order: Context AFTER User.
    assert header.index("User:") < header.index("Context:")


def test_header_context_env_limit_override(fake_home_with_real_session) -> None:
    """env CLAUDE_CODE_CONTEXT_LIMIT=1000000 → same tokens, percent vs 1M."""
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "X"},
        "context_window": {"total_input_tokens": CTX_TOKENS},
    })
    result = _run_main(stdin, tmp_path, extra_env={"CLAUDE_CODE_CONTEXT_LIMIT": "1000000"})
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    header = result.stdout.decode("utf-8").splitlines()[0]
    assert "| Context: 16K (2%)" in header, f"header: {header!r}"


def test_header_context_1m_model(fake_home_with_real_session) -> None:
    """No env; model display_name contains "[1m]" → divisor 1M."""
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "glm-5.3[1m]"},
        "context_window": {"total_input_tokens": CTX_TOKENS},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    header = result.stdout.decode("utf-8").splitlines()[0]
    assert "| Context: 16K (2%)" in header, f"header: {header!r}"


def test_header_context_env_beats_1m_model(fake_home_with_real_session) -> None:
    """env=500000 + "[1m]" model → env wins → "16K (3%)"."""
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "glm-5.3[1m]"},
        "context_window": {"total_input_tokens": CTX_TOKENS},
    })
    result = _run_main(stdin, tmp_path, extra_env={"CLAUDE_CODE_CONTEXT_LIMIT": "500000"})
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    header = result.stdout.decode("utf-8").splitlines()[0]
    assert "| Context: 16K (3%)" in header, f"header: {header!r}"


def test_header_context_jsonl_fallback(fake_home_with_real_session) -> None:
    """No context_window in payload (older CC) → jsonl-derived occupancy of
    the real session's last assistant event. Values are fixture-dependent,
    so assert the segment's shape and position, not exact numbers."""
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "MiniMax-M3"},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    header = result.stdout.decode("utf-8").splitlines()[0]
    m = re.search(r"\| Context: (\d+)K \((\d+)%\)$", header)
    assert m, f"header lacks a Context segment: {header!r}"
    # The real session's last assistant event has non-trivial usage.
    assert int(m.group(1)) > 0, f"context K should be positive: {header!r}"


def test_header_context_payload_wins_over_jsonl(fake_home_with_real_session) -> None:
    """Both sources available → payload wins (fresher; provided by CC)."""
    tmp_path, sid = fake_home_with_real_session
    # A distinct payload value (30000 → "30K (15%)" vs 200K) that the jsonl
    # fallback would never produce for this fixture.
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "X"},
        "context_window": {"total_input_tokens": 30_000},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    header = result.stdout.decode("utf-8").splitlines()[0]
    assert "| Context: 30K (15%)" in header, f"header: {header!r}"


def test_header_context_zero_without_session(fake_home_with_real_session) -> None:
    """Empty stdin → no session, no payload context → "Context: 0K (0%)"
    still renders (the field is unconditional in the header)."""
    tmp_path, _ = fake_home_with_real_session
    result = _run_main("", tmp_path)
    assert result.returncode == 0
    header = result.stdout.decode("utf-8").splitlines()[0]
    assert header.endswith("| Context: 0K (0%)"), f"header: {header!r}"


# ---------------------------------------------------------------------------
# 10. Dirless sessions (2026-08-24) — CC only creates `<sid>/` once the
# session spawns a subagent; sessions without one must still render the
# main-row table. Jsonl resolution: transcript_path payload → glob.
# ---------------------------------------------------------------------------

DIRLESS_SID = "99999999-8888-7777-6666-555555555555"

_DIRLESS_MAIN_LINES = [
    # event 1: in=1000, out=100, cache_create=2000, cache_read=3000
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"a"}],"model":"x","stop_reason":"end_turn","usage":{"input_tokens":1000,"cache_creation_input_tokens":2000,"cache_read_input_tokens":3000,"output_tokens":100}},"uuid":"d1","sessionId":"x","timestamp":"2026-08-24T21:00:00.000Z"}',
    # event 2 (last): in=800, out=50, cache_read=1000
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"b"}],"model":"x","stop_reason":"end_turn","usage":{"input_tokens":800,"cache_creation_input_tokens":0,"cache_read_input_tokens":1000,"output_tokens":50}},"uuid":"d2","sessionId":"x","timestamp":"2026-08-24T21:00:01.000Z"}',
]
# Expected aggregates: cum_in=1800 → "2K", cum_out=150 → "150",
# cum_cache_read=4000 → "4K"; last-event occupancy 800+0+1000=1800 → "2K (1%)".
_DIRLESS_EXPECTED_CELLS = ["2K", "150", "4K"]
# First event breakdown (the "start:" row): in=1000 → "1K", out=100 → "100",
# cache_read=3000 → "3K".
_DIRLESS_EXPECTED_START_CELLS = ["1K", "100", "3K"]


def _build_dirless_session(tmp_path: Path, sid: str) -> Path:
    """Create a jsonl WITHOUT its `<sid>/` directory — the subagentless
    session layout. Returns the jsonl path."""
    encoded = "dirless-project"
    jsonl = tmp_path / ".claude" / "projects" / encoded / f"{sid}.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text("\n".join(_DIRLESS_MAIN_LINES) + "\n")
    return jsonl


def test_dirless_session_via_transcript_path_renders_main_row(
    tmp_path: Path,
) -> None:
    """No `<sid>/` dir; payload carries transcript_path → table header +
    start row + main row render (no sum row, no agent rows), values from
    the jsonl, Context from the jsonl fallback."""
    jsonl = _build_dirless_session(tmp_path, DIRLESS_SID)
    stdin = json.dumps({
        "session_id": DIRLESS_SID,
        "model": {"display_name": "X"},
        "transcript_path": str(jsonl),
    })

    result = _run_main(stdin, tmp_path)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    lines = result.stdout.decode("utf-8").splitlines()
    assert len(lines) == 4, (
        f"expected 4 lines (header, labels, start, main): {lines!r}"
    )
    header, labels, start, main = lines
    # Context falls back to jsonl-derived occupancy of the LAST event.
    assert header.endswith("| Context: 2K (1%)"), f"header: {header!r}"
    # "| " table-row prefix, then the token labels and the always-visible
    # time block (no duration data in this pipeline stage → empty cells,
    # but the label row itself is non-empty)
    assert labels.split() == [
        "|", "in", "out", "cached", "work", "wait", "total",
    ], f"labels: {labels!r}"
    # start row carries the FIRST event's breakdown.
    start_cells = start.split()
    assert start_cells[:2] == ["|", "start:"], f"start row: {start!r}"
    assert start_cells[2:] == _DIRLESS_EXPECTED_START_CELLS, f"start row: {start!r}"
    cells = main.split()
    assert cells[:2] == ["|", "main:"], f"main row: {main!r}"
    # Token totals unchanged, then the three live duration cells — values
    # depend on wall-clock now (fixture stamps are in the past), so only
    # their HH:MM:SS shape is pinned here. Exact-value pins live in the
    # frozen-now tests of section 13.
    assert cells[2:5] == _DIRLESS_EXPECTED_CELLS, f"main row: {main!r}"
    assert len(cells) == 8 and all(
        _is_duration_cell(c) for c in cells[5:]
    ), f"main row must end with 3 duration cells: {main!r}"
    assert "sum:" not in result.stdout.decode("utf-8"), "no agents → no sum row"


def test_dirless_session_via_glob_renders_main_row(tmp_path: Path) -> None:
    """Same dirless layout but NO transcript_path in the payload (older CC)
    → the one-level projects glob still locates the jsonl; identical table."""
    _build_dirless_session(tmp_path, DIRLESS_SID)
    stdin = json.dumps({"session_id": DIRLESS_SID, "model": {"display_name": "X"}})

    result = _run_main(stdin, tmp_path)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    lines = result.stdout.decode("utf-8").splitlines()
    assert len(lines) == 4, f"expected 4 lines: {lines!r}"
    assert lines[0].endswith("| Context: 2K (1%)"), f"header: {lines[0]!r}"
    assert lines[2].split()[2:] == _DIRLESS_EXPECTED_START_CELLS, f"start: {lines[2]!r}"
    main_cells = lines[3].split()
    assert main_cells[2:5] == _DIRLESS_EXPECTED_CELLS, f"main: {lines[3]!r}"
    assert all(_is_duration_cell(c) for c in main_cells[5:]), (
        f"main row must end with duration cells: {lines[3]!r}"
    )


def test_dirless_session_skips_agents_cache_write(tmp_path: Path) -> None:
    """No session dir → no agents to cache; agents_<sid>.json must NOT be
    created (no data/ litter), while main_<sid>.json IS written."""
    _build_dirless_session(tmp_path, DIRLESS_SID)
    stdin = json.dumps({
        "session_id": DIRLESS_SID,
        "model": {"display_name": "X"},
        "context_window": {"total_input_tokens": 1000},
    })

    result = _run_main(stdin, tmp_path)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    data_dir = tmp_path / ".claude" / "status_line" / "data"
    assert not (data_dir / f"agents_{DIRLESS_SID}.json").exists()
    assert (data_dir / f"main_{DIRLESS_SID}.json").exists()


def test_no_jsonl_anywhere_still_header_only(tmp_path: Path) -> None:
    """Session id with neither a session dir, nor a transcript_path, nor a
    globbable jsonl → header-only degrade (the historical no-dir behavior)."""
    (tmp_path / ".claude" / "projects" / "empty-project").mkdir(parents=True)
    stdin = json.dumps({"session_id": "no-such-session", "model": {"display_name": "X"}})

    result = _run_main(stdin, tmp_path)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    lines = result.stdout.decode("utf-8").splitlines()
    assert len(lines) == 1, f"header-only expected, got: {lines!r}"
    assert lines[0].endswith("| Context: 0K (0%)"), f"header: {lines[0]!r}"


# ---------------------------------------------------------------------------
# 11. Duplicate session dirs (2026-08-26, plan
#     20260826-merge-subagents-across-session-dirs) — the same session id can
#     legitimately live in TWO encoded project dirs (main checkout + worktree
#     copy); agents must merge across both, dedup by agentId, and the empty
#     worktree dir sorting first (bug eacc81d9) must not hide agents.
# ---------------------------------------------------------------------------

MERGE_SID = "51f0c0de-1111-4222-8333-444444444444"
SPLIT_SID = "62a1d1ef-2222-4333-8444-555555555555"

# Single assistant end_turn event — minimal main jsonl (no queue events, no
# tool_use positions; every agent sorts via the sentinel, order irrelevant).
_SPLIT_MAIN_LINES = [
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"a"}],"model":"x","stop_reason":"end_turn","usage":{"input_tokens":100,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":10}},"uuid":"m1","sessionId":"x","timestamp":"2026-08-26T10:00:00.000Z"}',
]


def _ok_agent_jsonl() -> str:
    """Agent transcript ending in end_turn → detect_status says [ok]."""
    return (
        '{"type":"user","message":{"role":"user","content":"x"},"uuid":"u1","sessionId":"x","timestamp":"2026-08-26T10:00:00.000Z"}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"model":"x","stop_reason":"end_turn","usage":{"input_tokens":10,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":5}},"uuid":"a1","sessionId":"x","timestamp":"2026-08-26T10:00:00.500Z"}'
    )


def _agent_meta(description: str, tool_use_id: str) -> str:
    return json.dumps({
        "agentType": "general-purpose",
        "description": description,
        "toolUseId": tool_use_id,
        "spawnDepth": 1,
    })


def _build_split_session(tmp_path: Path, sid: str) -> Path:
    """Build the eacc81d9 bug layout: a worktree project dir whose name sorts
    FIRST ('-' prefix < any letter) holding only tool-results/ (no
    subagents/), plus the real project dir with the agents and main jsonl.

    Returns the main jsonl path (for the payload's transcript_path)."""
    worktree_session = (
        tmp_path / ".claude" / "projects" / "-worktree-copy-first" / sid
    )
    (worktree_session / "tool-results").mkdir(parents=True)
    _build_synth_session(
        tmp_path,
        sid,
        _SPLIT_MAIN_LINES,
        [
            ("agent-real111", _ok_agent_jsonl(), _agent_meta("Real: first agent", "tr1")),
            ("agent-real222", _ok_agent_jsonl(), _agent_meta("Real: second agent", "tr2")),
        ],
        encoded="real-project",
    )
    return tmp_path / ".claude" / "projects" / "real-project" / f"{sid}.jsonl"


def _build_merge_layout(tmp_path: Path, sid: str) -> Path:
    """Two project dirs carrying the same <sid>/subagents/ trees: agents
    split across them, one agentId in BOTH (transcript dir's copy must win
    the dedup). Returns the main dir's transcript jsonl path."""
    _build_synth_session(
        tmp_path,
        sid,
        _SPLIT_MAIN_LINES,
        [
            ("agent-main111", _ok_agent_jsonl(), _agent_meta("Main: only here", "tm1")),
            ("agent-shared", _ok_agent_jsonl(), _agent_meta("Shared: main dir wins", "tm2")),
        ],
        encoded="merge-main-project",
    )
    _build_synth_session(
        tmp_path,
        sid,
        _SPLIT_MAIN_LINES,
        [
            ("agent-copy111", _ok_agent_jsonl(), _agent_meta("Copy: only here", "tc1")),
            ("agent-shared", _ok_agent_jsonl(), _agent_meta("Shared: copy loses", "tc2")),
        ],
        encoded="merge-copy-project",
    )
    return tmp_path / ".claude" / "projects" / "merge-main-project" / f"{sid}.jsonl"


def test_merge_agents_across_duplicate_session_dirs(tmp_path: Path) -> None:
    """Two project dirs both carry <sid>/subagents/; agents are split across
    them and one agentId exists in BOTH → output has every agent exactly once;
    the shared agentId resolves to the transcript dir's copy (first dir wins).
    """
    transcript = _build_merge_layout(tmp_path, MERGE_SID)
    stdin = json.dumps({
        "session_id": MERGE_SID,
        "model": {"display_name": "X"},
        "transcript_path": str(transcript),
    })

    result = _run_main(stdin, tmp_path)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    output = result.stdout.decode("utf-8")
    lines = output.splitlines()
    # header + table header + start + sum + main + 3 agents = 8
    assert len(lines) == 8, f"expected 8 lines (3 agents), got {len(lines)}: {lines!r}"
    assert "Main: only here" in output, f"main-dir agent missing:\n{output!r}"
    assert "Copy: only here" in output, f"copy-dir agent missing:\n{output!r}"
    # Shared agentId: the transcript dir's copy wins, appears exactly once.
    assert "Shared: main dir wins" in output, f"shared agent should come from main dir:\n{output!r}"
    assert "Shared: copy loses" not in output, f"copy dir must lose the dedup:\n{output!r}"
    assert output.count("Shared:") == 1, f"shared agent rendered more than once:\n{output!r}"


def test_empty_worktree_dir_sorts_first_agents_still_render(tmp_path: Path) -> None:
    """Bug eacc81d9 repro: the worktree project dir sorts FIRST (its encoded
    name starts with '-') and contains only tool-results/ — no subagents/.
    Agents from the real dir must still render."""
    transcript = _build_split_session(tmp_path, SPLIT_SID)
    stdin = json.dumps({
        "session_id": SPLIT_SID,
        "model": {"display_name": "X"},
        "transcript_path": str(transcript),
    })

    result = _run_main(stdin, tmp_path)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    output = result.stdout.decode("utf-8")
    lines = output.splitlines()
    # header + table header + start + sum + main + 2 agents = 7
    assert len(lines) == 7, f"expected 7 lines (2 agents), got {len(lines)}: {lines!r}"
    assert "Real: first agent" in output, f"real-dir agents missing:\n{output!r}"
    assert "Real: second agent" in output, f"real-dir agents missing:\n{output!r}"


def test_stale_empty_agents_cache_self_heals(tmp_path: Path) -> None:
    """The buggy code wrote agents_<sid>.json = {} for sessions whose only
    found dir was an empty worktree copy. Pre-seed that artifact and verify
    the fixed run still renders agents AND rewrites the cache non-empty."""
    transcript = _build_split_session(tmp_path, SPLIT_SID)
    data_dir = tmp_path / ".claude" / "status_line" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    agents_cache_path = data_dir / f"agents_{SPLIT_SID}.json"
    agents_cache_path.write_text("{}", encoding="utf-8")

    stdin = json.dumps({
        "session_id": SPLIT_SID,
        "model": {"display_name": "X"},
        "transcript_path": str(transcript),
    })
    result = _run_main(stdin, tmp_path)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    output = result.stdout.decode("utf-8")
    assert "Real: first agent" in output, f"agents must render despite empty cache:\n{output!r}"
    assert "Real: second agent" in output, f"agents must render despite empty cache:\n{output!r}"
    loaded = json.loads(agents_cache_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    assert len(loaded) == 2, f"cache must be rewritten non-empty, got: {loaded!r}"


def test_merge_agents_without_transcript_path(tmp_path: Path) -> None:
    """Older CC payloads carry no transcript_path → dir resolution is pure
    glob; the cross-dir merge must still happen (a regression merging only
    when transcript_path is present would otherwise pass the suite).
    main jsonl is located via the first glob dir's sibling."""
    _build_merge_layout(tmp_path, MERGE_SID)
    stdin = json.dumps({"session_id": MERGE_SID, "model": {"display_name": "X"}})

    result = _run_main(stdin, tmp_path)

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    output = result.stdout.decode("utf-8")
    lines = output.splitlines()
    # header + table header + start + sum + main + 3 agents = 8
    assert len(lines) == 8, f"expected 8 lines (3 agents), got {len(lines)}: {lines!r}"
    assert "Main: only here" in output, f"main-dir agent missing:\n{output!r}"
    assert "Copy: only here" in output, f"copy-dir agent missing:\n{output!r}"
    # shared agentId rendered exactly once (either copy — no transcript
    # anchor here, glob order decides; the dedup itself is what matters)
    assert output.count("Shared:") == 1, f"shared agent rendered more than once:\n{output!r}"


def test_merge_session_second_call_uses_persisted_cache(tmp_path: Path) -> None:
    """The hook's dominant real-world mode is cache-hit: the SECOND
    invocation must re-read the MERGED snapshot set (persisted across two
    dirs, incl. the same-agentId dedup choice) and render identically."""
    transcript = _build_merge_layout(tmp_path, MERGE_SID)
    stdin = json.dumps({
        "session_id": MERGE_SID,
        "model": {"display_name": "X"},
        "transcript_path": str(transcript),
    })
    agents_cache_path = (
        tmp_path / ".claude" / "status_line" / "data" / f"agents_{MERGE_SID}.json"
    )

    first = _run_main(stdin, tmp_path)
    assert first.returncode == 0, first.stderr.decode("utf-8", "replace")
    assert first.stdout.decode("utf-8").count("Shared:") == 1
    assert agents_cache_path.exists(), "merged agents cache must be written"
    loaded = json.loads(agents_cache_path.read_text(encoding="utf-8"))
    assert len(loaded) == 3, f"cache must hold the merged set, got: {sorted(loaded)!r}"

    second = _run_main(stdin, tmp_path)

    assert second.returncode == 0, second.stderr.decode("utf-8", "replace")
    # Durations are masked: live-now cells legitimately grow between the
    # two invocations; everything else must be byte-identical.
    assert _mask_durations(second.stdout.decode("utf-8")) == _mask_durations(
        first.stdout.decode("utf-8")
    ), (
        "cache-hit invocation must render identically:\n"
        f"first={first.stdout.decode('utf-8')!r}\nsecond={second.stdout.decode('utf-8')!r}"
    )
    reloaded = json.loads(agents_cache_path.read_text(encoding="utf-8"))
    assert len(reloaded) == 3, f"cache must still hold the merged set: {sorted(reloaded)!r}"


def test_hook_runtime_smoke_on_multi_project_tree(tmp_path: Path) -> None:
    """Generous latency canary for the hook's hottest path (plan Task 5
    budget: <2x the pre-merge runtime, ~0.4s on the real tree). A full
    invocation over a synthetic multi-project tree must stay far below
    5s — an accidental return to full-tree recursive globbing or per-dir
    rescanning would blow past it. Absolute bound (not a ratio) on
    purpose: ratios are flaky on shared machines; 5s is ~10x the
    observed worst case, so it only trips on real regressions."""
    target_sid = "cccc0000-3000-4000-8000-00000000000c"
    for proj in range(25):
        encoded = f"smoke-project-{proj:02d}"
        for s in range(4):
            sid = target_sid if s == 0 else f"cccc{proj:02d}{s:02d}-3000-4000-8000-{proj:012d}"
            session_dir = tmp_path / ".claude" / "projects" / encoded / sid
            subagents = session_dir / "subagents"
            subagents.mkdir(parents=True)
            (session_dir / "tool-results").mkdir()
            main_jsonl = session_dir.parent / f"{sid}.jsonl"
            main_jsonl.write_text("\n".join(_SPLIT_MAIN_LINES) + "\n")
            for a in range(3):
                (subagents / f"agent-smoke{a}.jsonl").write_text(_ok_agent_jsonl())
                (subagents / f"agent-smoke{a}.meta.json").write_text(
                    _agent_meta(f"Smoke {proj}-{s}-{a}", f"ts{proj}{s}{a}")
                )
    stdin = json.dumps({
        "session_id": target_sid,
        "model": {"display_name": "X"},
        "transcript_path": str(
            tmp_path / ".claude" / "projects" / "smoke-project-00" / f"{target_sid}.jsonl"
        ),
    })

    start = time.monotonic()
    result = _run_main(stdin, tmp_path)
    elapsed = time.monotonic() - start

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    output = result.stdout.decode("utf-8")
    assert "Smoke 0-0-1" in output, f"target session's agents must render:\n{output!r}"
    assert elapsed < 5.0, f"hook invocation took {elapsed:.2f}s on the synthetic tree (>5s budget)"


# ---------------------------------------------------------------------------
# 12. prices.json / model+cost columns (20260826 plan, Task 5). prices.json
#     is written into the FAKE home so the subprocess resolves
#     _PRICES_PATH = <home>/.claude/status_line/prices.json hermetically
#     (HOME/USERPROFILE are overridden in _run_main). ANTHROPIC_BASE_URL is
#     popped by _run_main unless a test sets it via extra_env.
# ---------------------------------------------------------------------------

# Uniform prices (in=out=cache=1 per 1M tokens, "$") keep the expected cost
# arithmetic trivial: cost = (in + out + cached) / 1e6, "$"-prefixed.
_UNIT_PRICES = [
    {"model": "kimi-k3", "in": 1, "out": 1, "cache": 1, "per": 1_000_000, "units": "$"},
    {"model": "glm-5.3", "in": 1, "out": 1, "cache": 1, "per": 1_000_000, "units": "$"},
]
# Same numbers, but keyed "<model>@api.z.ai" — only reachable when the child
# sees ANTHROPIC_BASE_URL with that hostname (price_for's "@host" chain).
_UNIT_PRICES_AT_HOST = [
    {"model": "kimi-k3@api.z.ai", "in": 1, "out": 1, "cache": 1, "per": 1_000_000, "units": "$"},
    {"model": "glm-5.3@api.z.ai", "in": 1, "out": 1, "cache": 1, "per": 1_000_000, "units": "$"},
]

# Fixture main jsonl per-model cumulative totals (verified against the file):
#   kimi-k3:     in=6613240 out=265021 cached=36263168
#   glm-5.3:     in=414451  out=16739  cached=7183936
#   <synthetic>: all zero → skipped at render
# format_tokens → "6.6M"/"265K"/"36.3M" and "414K"/"17K"/"7.2M".
# Cost with _UNIT_PRICES = (in+out+cached)/1e6 → 43.141429 → "$43.1" and
# 7.615126 → "$7.6".
_MAIN_KIMI_ROW = ["|", "main:", "kimi-k3", "6.6M", "265K", "36.3M", "$43.1"]
_MAIN_GLM_ROW = ["|", "glm-5.3", "414K", "17K", "7.2M", "$7.6"]

# With prices the fixture renders 45 lines: header + table header + start
# + sum(2 models) + main(2 models) + 38 agent rows (27 kimi-k3, 6 glm-5.3,
# 5 zero-token agents as single zero rows with an empty model cell).
_PRICES_LINE_COUNT = 45
_NO_PRICES_LINE_COUNT = 43


def _write_prices(home: Path, payload: object) -> Path:
    """Write a prices payload to <home>/.claude/status_line/prices.json."""
    prices_path = home / ".claude" / "status_line" / "prices.json"
    prices_path.parent.mkdir(parents=True, exist_ok=True)
    prices_path.write_text(json.dumps(payload), encoding="utf-8")
    return prices_path


def test_prices_plain_key_adds_model_and_cost_columns(
    fake_home_with_real_session,
) -> None:
    """prices.json with PLAIN model keys (no env) → model/cost columns render:
    table header gains the labels, sum/main expand per model (first-appearance
    order: kimi-k3 then glm-5.3), costs come from the unit prices, and the
    zero-token <synthetic> record is not displayed anywhere."""
    tmp_path, sid = fake_home_with_real_session
    _write_prices(tmp_path, _UNIT_PRICES)

    stdin = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    output = result.stdout.decode("utf-8")
    lines = output.splitlines()
    assert len(lines) == _PRICES_LINE_COUNT, (
        f"expected {_PRICES_LINE_COUNT} lines, got {len(lines)}; "
        f"first 8: {lines[:8]}"
    )
    # Table header carries the model and cost labels alongside in/out/cached
    # plus the always-visible work/wait/total block (no time data flows
    # through the pipeline until the orchestrator wires it — empty cells).
    assert lines[1].split() == [
        "|", "model", "in", "out", "cached", "cost", "work", "wait", "total",
    ], (
        f"table header: {lines[1]!r}"
    )
    # The start row is a reference row (not part of sum) but carries the
    # first event's model and its priced cost like every other row.
    assert lines[2].split()[:2] == ["|", "start:"], f"start row: {lines[2]!r}"
    assert len(lines[2].split()) == 7, (
        f"start row must carry model + in/out/cached + cost cells: {lines[2]!r}"
    )
    # sum group: two model rows, label only on the first, both with costs.
    # Since Task 6 wires live durations, time cells ride ONLY a group's
    # FIRST row (continuations rstrip to their cost), so each row's cost is
    # located after trimming an optional trailing HH:MM:SS run.
    def _cost_of(cells: list) -> str:
        if len(cells) >= 3 and all(_is_duration_cell(c) for c in cells[-3:]):
            return cells[-4]
        return cells[-1]

    assert lines[3].split()[:3] == ["|", "sum:", "kimi-k3"], f"sum row 1: {lines[3]!r}"
    assert _cost_of(lines[3].split()).startswith("$"), f"sum row 1 cost: {lines[3]!r}"
    assert lines[4].split()[:2] == ["|", "glm-5.3"], f"sum row 2: {lines[4]!r}"
    assert "sum:" not in lines[4], f"label must ride the first row only: {lines[4]!r}"
    assert _cost_of(lines[4].split()).startswith("$"), f"sum row 2 cost: {lines[4]!r}"
    # main group: per-model cumulative totals + computed costs.
    for index, expected in ((5, _MAIN_KIMI_ROW), (6, _MAIN_GLM_ROW)):
        got = lines[index].split()
        assert got[:7] == expected, f"main row: {lines[index]!r}"
        assert all(_is_duration_cell(c) for c in got[7:]), (
            f"main row must end with 3 duration cells: {lines[index]!r}"
        )
    # 38 agent rows, one per agent: single-model agents collapse to one row.
    agent_lines = lines[7:]
    assert len(agent_lines) == 38, f"expected 38 agent rows, got {len(agent_lines)}"
    assert all(line.startswith("| [") for line in agent_lines)
    assert sum(1 for l in agent_lines if "kimi-k3" in l.split()) == 27
    assert sum(1 for l in agent_lines if "glm-5.3" in l.split()) == 6
    # Zero-token <synthetic> records are never displayed (main or agents).
    assert "<synthetic>" not in output
    # Both displayed models are priced → no n/a cost cells in any table row
    # (the header's "User: n/a" is not a table row — skip line 0).
    assert all("n/a" not in line.split() for line in lines[1:]), (
        "unpriced model leaked an n/a cost cell"
    )


def test_prices_host_key_matches_via_env(fake_home_with_real_session) -> None:
    """prices.json keyed "<model>@api.z.ai" + child env
    ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic → the @host entries
    match and the rendered costs are identical to the plain-key variant."""
    tmp_path, sid = fake_home_with_real_session
    _write_prices(tmp_path, _UNIT_PRICES_AT_HOST)

    stdin = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    result = _run_main(
        stdin,
        tmp_path,
        extra_env={"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"},
    )
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    lines = result.stdout.decode("utf-8").splitlines()
    assert len(lines) == _PRICES_LINE_COUNT, f"first 8: {lines[:8]}"
    for index, expected in ((5, _MAIN_KIMI_ROW), (6, _MAIN_GLM_ROW)):
        got = lines[index].split()
        assert got[:7] == expected, f"main row: {lines[index]!r}"
        assert all(_is_duration_cell(c) for c in got[7:])
    assert all("n/a" not in line.split() for line in lines[1:])


def test_prices_host_key_without_env_is_na(fake_home_with_real_session) -> None:
    """@host-keyed prices WITHOUT ANTHROPIC_BASE_URL in the child env →
    provider_host()="" → no entry matches → cost cells render "n/a" (columns
    still present: the prices file itself is valid).

    This also proves _run_main's env hygiene: the machine's own
    ANTHROPIC_BASE_URL must not leak into the subprocess and silently match
    the @host keys."""
    tmp_path, sid = fake_home_with_real_session
    _write_prices(tmp_path, _UNIT_PRICES_AT_HOST)

    stdin = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    lines = result.stdout.decode("utf-8").splitlines()
    assert len(lines) == _PRICES_LINE_COUNT, f"first 8: {lines[:8]}"
    for index, expected in ((5, _MAIN_KIMI_ROW), (6, _MAIN_GLM_ROW)):
        got = lines[index].split()
        assert got[:7] == expected[:-1] + ["n/a"], (
            f"main row should be n/a: {lines[index]!r}"
        )
        assert all(_is_duration_cell(c) for c in got[7:])


def test_prices_absent_no_columns(fake_home_with_real_session) -> None:
    """No prices.json in the (fake) home → no model/cost columns; the layout
    is the pre-model-columns one (43 lines, plain in/out/cached header)."""
    tmp_path, sid = fake_home_with_real_session
    assert not (tmp_path / ".claude" / "status_line" / "prices.json").exists()

    stdin = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    lines = result.stdout.decode("utf-8").splitlines()
    assert len(lines) == _NO_PRICES_LINE_COUNT, f"first 8: {lines[:8]}"
    assert lines[1].split() == [
        "|", "in", "out", "cached", "work", "wait", "total",
    ], f"header: {lines[1]!r}"
    # No per-model expansion either: one flat row per group.
    assert lines[3].split()[:2] == ["|", "sum:"], f"sum row: {lines[3]!r}"
    assert lines[4].split()[:2] == ["|", "main:"], f"main row: {lines[4]!r}"


def test_prices_broken_file_no_columns(fake_home_with_real_session) -> None:
    """A malformed prices.json is treated as absent → no model/cost columns,
    exit 0 (the hook must not break on a corrupt file)."""
    tmp_path, sid = fake_home_with_real_session
    _write_prices(tmp_path, "not json {")

    stdin = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    lines = result.stdout.decode("utf-8").splitlines()
    assert len(lines) == _NO_PRICES_LINE_COUNT, f"first 8: {lines[:8]}"
    assert lines[1].split() == [
        "|", "in", "out", "cached", "work", "wait", "total",
    ], f"header: {lines[1]!r}"


SYNTH_PRICES_SID = "12345678-1234-1234-1234-123456789012"


def test_synth_prices_per_model_rows_and_costs(tmp_path: Path) -> None:
    """Hermetic end-to-end prices coverage (no real-session fixture): a
    synthetic multi-model main jsonl + one agent, priced with the
    example-file numbers. Pins the exact per-model rows, byte-exact
    layout, and the cost arithmetic — this test cannot break when the
    gitignored live fixture is extended/re-captured (the fixture-based
    tests above keep their exact values but skip when it is absent).
    """
    main_lines = [
        # e1 glm-5.3: in=10000 out=5000 cache_read=20000 (also the start row)
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"a"}],"model":"glm-5.3","stop_reason":"tool_use","usage":{"input_tokens":10000,"cache_creation_input_tokens":0,"cache_read_input_tokens":20000,"output_tokens":5000}},"uuid":"m1","timestamp":"2026-08-26T20:00:00.000Z"}',
        # e2 kimi-k3: in=2000000 out=100000 cache_read=0
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"b"}],"model":"kimi-k3","stop_reason":"end_turn","usage":{"input_tokens":2000000,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":100000}},"uuid":"m2","timestamp":"2026-08-26T20:00:01.000Z"}',
    ]
    agent_jsonl = (
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"model":"glm-5.3","stop_reason":"end_turn","usage":{"input_tokens":12000,"cache_creation_input_tokens":0,"cache_read_input_tokens":100000,"output_tokens":4000}},"uuid":"a1","timestamp":"2026-08-26T20:00:02.000Z"}'
    )
    agent_meta = json.dumps({
        "agentType": "general-purpose",
        "description": "Synth agent",
        "toolUseId": "toolu_synth",
    })
    _build_synth_session(
        tmp_path,
        SYNTH_PRICES_SID,
        main_lines,
        [("agent-aaa111", agent_jsonl, agent_meta)],
    )
    # Example prices: glm-5.3 in credits (per 10K), kimi-k3 in $ (per 1M).
    _write_prices(
        tmp_path,
        [
            {"model": "glm-5.3", "in": 6.9, "out": 24, "cache": 1.7,
             "per": 10000, "units": "credits"},
            {"model": "kimi-k3", "in": 3, "out": 15, "cache": 0.3,
             "per": 1000000, "units": "$"},
        ],
    )

    stdin = json.dumps({
        "session_id": SYNTH_PRICES_SID,
        "model": {"display_name": "X"},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    lines = result.stdout.decode("utf-8").splitlines()
    # header + labels + start + sum(2) + main(2) + agent(1) = 8 lines
    assert len(lines) == 8, f"first 8: {lines[:8]}"
    # Structural pin of every table row (header line skipped — it carries
    # the machine's branch name). Costs: main glm
    # (10000*6.9+5000*24+20000*1.7)/10000 = 22.3 credits; kimi
    # (2000000*3+100000*15)/1e6 = 7.5 → $7.5; sum glm
    # (22000*6.9+9000*24+120000*1.7)/10000 = 57.18 → 57.2 credits;
    # agent glm (12000*6.9+4000*24+100000*1.7)/10000 = 34.88 → 34.9.
    # Since the orchestrator passes now=time.time(), each group's FIRST row
    # also ends with three live HH:MM:SS cells whose values depend on
    # wall-clock distance to the fixture stamps — only shape is pinned.
    # Continuation per-model rows keep EMPTY time cells (they are rstripped
    # away), and the reference start row never carries time cells at all.
    # Row tuples: (label-or-None, fixed tail cells, has_duration_tail).
    # [deviation] Previously this table was byte-exact; live durations made
    # the trailing cells (and eventually the column WIDTHS) wall-clock
    # dependent.
    assert lines[1].split() == [
        "|", "model", "in", "out", "cached", "cost", "work", "wait", "total",
    ], f"labels: {lines[1]!r}"
    expected_rows = [
        ("start:", ["glm-5.3", "10K", "5K", "20K", "22.3", "credits"], False),
        ("sum:", ["glm-5.3", "22K", "9K", "120K", "57.2", "credits"], True),
        (None, ["kimi-k3", "2.0M", "100K", "0", "$7.5"], False),
        ("main:", ["glm-5.3", "10K", "5K", "20K", "22.3", "credits"], True),
        (None, ["kimi-k3", "2.0M", "100K", "0", "$7.5"], False),
        # The icon AND the description form the group label together.
        ("[ok] Synth agent", ["glm-5.3", "12K", "4K", "100K", "34.9",
                              "credits"], True),
    ]
    for raw, (label, tail_cells, has_durations) in zip(lines[2:], expected_rows):
        got = raw.split()
        body = got[1:]
        # Group label rides the row start; descriptions may contain spaces,
        # so match it word-wise.
        if label is not None:
            label_parts = label.split()
            assert body[: len(label_parts)] == label_parts, (
                f"row {raw!r}: label {label!r} not at row start"
            )
            body = body[len(label_parts):]
        # First rows of a group close with three positive live duration
        # cells (fixture stamps lie in the past); cut them off before
        # comparing the fixed model/token/cost tail.
        if has_durations:
            assert len(body) >= 3 and all(
                _is_duration_cell(c) for c in body[-3:]
            ), f"row must end with 3 HH:MM:SS cells: {raw!r}"
            # Zeros are legitimate here (the agent transcript is a single
            # stamped event → a zero-length lifetime renders 00:00:00);
            # strictly-positive durations are pinned by the frozen-now and
            # real-session tests instead.
            body = body[:-3]
        assert body == list(tail_cells), (
            f"row {raw!r}: fixed cells {body!r} != {list(tail_cells)!r}"
        )


# ---------------------------------------------------------------------------
# 13. Orchestrator time columns (20260827 plan, Task 6): _main_unsafe(now=…)
#     unions main turns + agent lifetimes into session work/wait/total and
#     injects per-agent durations AFTER the agents-cache write.
#
#     Frozen-now coverage runs _main_unsafe IN-PROCESS (per the plan's
#     Testing Strategy: monkeypatch cannot cross a subprocess boundary, so
#     wall-clock freezing requires the in-process route with patched stdin +
#     capsys). The real-fixture test below stays in the historical
#     subprocess format because its assertions are invariant to `now`.
# ---------------------------------------------------------------------------

_FROZEN_BASE = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def _frozen_iso(offset: float) -> str:
    """ISO-8601 Z stamp `offset` seconds after the frozen base."""
    dt = _FROZEN_BASE + timedelta(seconds=offset)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _fzep(offset: float) -> float:
    """Epoch expectation for a frozen-base offset (via _parse_ts — no epoch
    number is ever hardcoded)."""
    return _parse_ts(_frozen_iso(offset))


def _fz_user(offset: float, text: str) -> str:
    return json.dumps({
        "type": "user",
        "timestamp": _frozen_iso(offset),
        "message": {"role": "user", "content": text},
        "uuid": f"fu{offset:.0f}",
    })


def _fz_assistant(
    offset: float, stop: str = "end_turn", in_tok: int = 100, out_tok: int = 10
) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": _frozen_iso(offset),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "working"}],
            "model": "kimi-k3",
            "stop_reason": stop,
            "usage": {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
        "uuid": f"fa{offset:.0f}",
    })


def _fz_agent_jsonl(user_off: float, assist_off: float) -> str:
    """Two-event agent transcript: user prompt then an end_turn assistant →
    detect_status says [ok]; lifetime = [user_off, assist_off]."""
    return (
        _fz_user(user_off, "sub-agent task")
        + "\n"
        + _fz_assistant(assist_off, "end_turn", in_tok=50, out_tok=5)
        + "\n"
    )


def _fz_main_done_turn() -> list[str]:
    """Main transcript with ONE finished turn: prompt at t=0 answered by an
    end_turn assistant at t=20 — afterwards the session is idle (turn
    closed, nothing extends toward now)."""
    return [
        _fz_user(0, "kick things off"),
        _fz_assistant(20, "end_turn"),
    ]


# Frozen geometry shared by both orchestrator tests (all in seconds):
#   main turn work ................. 20   ([t=0, t=20])
#   background agents' lifetimes ... 240  ([t=60, t=300])
#   frozen now ..................... t=1200
# Session union work = 20 + 240 = 260 → "00:04:20";
# total = 1200 → "00:20:00"; wait = 940 → "00:15:40".
_FROZEN_NOW_OFFSET = 1200
_FROZEN_SESSION_CELLS = ["00:04:20", "00:15:40", "00:20:00"]
_FROZEN_AGENT_CELLS = ["00:04:00", "00:00:00", "00:04:00"]

FROZEN_SID_IDLE = "71d1e100-0000-4000-8000-0000000000a1"
FROZEN_SID_PARALLEL = "71d1e100-0000-4000-8000-0000000000b2"


def _run_frozen_now(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    sid: str,
    main_lines: list[str],
    agent_files: list[tuple[str, str, str]],
) -> tuple[int, list[str]]:
    """Build a synthetic session, then invoke `_main_unsafe(now=…)`
    IN-PROCESS with the clock effectively frozen (now is passed explicitly).

    Hermeticity inside one pytest process:
      - Path.home → tmp_path (projects tree + status_line data dir resolve
        under it — mirrors what the HOME-env override does for subprocess
        runs in _run_main);
      - _PRICES_PATH is rebound under tmp_path — the import-time constant
        still points at the DEVELOPER'S real prices.json and would otherwise
        leak columns into these runs;
      - sys.stdin → StringIO with the JSON payload (parse_stdin contract);
      - stdout goes through capsys.

    The parse_stdin branch probe may shell out to real git in the repo cwd —
    harmless, only the header's Branch segment consumes the result.
    """
    _build_synth_session(tmp_path, sid, main_lines, agent_files)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        status_line,
        "_PRICES_PATH",
        tmp_path / ".claude" / "status_line" / "prices.json",
    )
    payload = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc = status_line._main_unsafe(now=_fzep(_FROZEN_NOW_OFFSET))
    captured = capsys.readouterr()
    return rc, captured.out.splitlines()


def test_frozen_background_agent_fills_main_idle_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """THE rule: a background agent living entirely inside main's idle gap
    turns that waiting into WORK via the union (its whole lifetime counts
    once, even though main's own turns ended long before)."""
    rc, lines = _run_frozen_now(
        monkeypatch,
        capsys,
        tmp_path,
        FROZEN_SID_IDLE,
        _fz_main_done_turn(),
        [("agent-bgfill", _fz_agent_jsonl(60, 300),
          _agent_meta("Bg: filler", "toolu_bg"))],
    )

    assert rc == 0, f"non-zero exit; lines[:2]={lines[:2]!r}"
    # header, labels, start, sum, main, 1 agent = 6 lines
    assert len(lines) == 6, f"unexpected line count: {lines!r}"
    assert lines[1].split() == [
        "|", "in", "out", "cached", "work", "wait", "total",
    ], f"labels: {lines[1]!r}"

    sum_cells = next(l for l in lines if l.startswith("| sum:")).split()
    main_cells = next(l for l in lines if l.startswith("| main:")).split()
    # Identical triples on both rows — waiting on agents already counts as
    # main's work (union consequence agreed in the plan Overview).
    assert sum_cells[-3:] == _FROZEN_SESSION_CELLS, f"sum row: {sum_cells!r}"
    assert main_cells[-3:] == _FROZEN_SESSION_CELLS, f"main row: {main_cells!r}"
    work, wait_s, total_s = (_hms_seconds(c) for c in sum_cells[-3:])
    # Without the agent, work would have stopped at 20s (main's own turn).
    # The union raised it to 260 = 20 + 240 — the agent's life became work.
    assert work == 260 > 20, f"union did not fold agent idle-gap time in: {work}s"
    assert work + wait_s == total_s, f"invariant broken: {work}+{wait_s}!={total_s}"
    assert total_s == _FROZEN_NOW_OFFSET

    # Per-agent cells ride the agent's row: dur 240 / wait 0 / total 240.
    agent_cells = next(l for l in lines if "[ok]" in l).split()
    assert agent_cells[-3:] == _FROZEN_AGENT_CELLS, f"agent row: {agent_cells!r}"

    # The injected durations are TRANSIENT: nothing but the four scan fields
    # reaches the persisted agents cache.
    cache_path = (
        tmp_path / ".claude" / "status_line" / "data" / f"agents_{FROZEN_SID_IDLE}.json"
    )
    loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    entry = loaded["agent-bgfill"]
    assert all(f not in entry for f in ("time_work", "time_wait", "time_total")), (
        f"transient durations leaked into cache: {sorted(entry)!r}"
    )
    assert entry["ts_first"] == _fzep(60)
    assert entry["ts_last"] == _fzep(300)


def test_frozen_parallel_agents_union_does_not_double_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Two agents covering the SAME wall-clock window add their duration to
    the union ONCE: session work stays 260 (= main 20 + window 240), never
    500 (naive per-agent summation)."""
    agents = [
        ("agent-parA", _fz_agent_jsonl(60, 300),
         _agent_meta("Par: alpha", "toolu_pa")),
        ("agent-parB", _fz_agent_jsonl(60, 300),
         _agent_meta("Par: beta", "toolu_pb")),
    ]
    rc, lines = _run_frozen_now(
        monkeypatch, capsys, tmp_path, FROZEN_SID_PARALLEL,
        _fz_main_done_turn(), agents,
    )

    assert rc == 0, f"non-zero exit; lines[:2]={lines[:2]!r}"
    # header, labels, start, sum, main, 2 agents = 7 lines
    assert len(lines) == 7, f"unexpected line count: {lines!r}"
    sum_cells = next(l for l in lines if l.startswith("| sum:")).split()
    main_cells = next(l for l in lines if l.startswith("| main:")).split()
    assert sum_cells[-3:] == _FROZEN_SESSION_CELLS, (
        f"parallel windows doubled: {sum_cells!r}"
    )
    assert main_cells[-3:] == sum_cells[-3:], "main/sum divergence"
    work, wait_s, total_s = (_hms_seconds(c) for c in sum_cells[-3:])
    assert work == 260, f"expected single-count union (260s), got {work}s"
    assert work + wait_s == total_s

    # EACH agent still shows its OWN full window as its personal duration.
    par_alpha = next(l for l in lines if "Par: alpha" in l).split()
    par_beta = next(l for l in lines if "Par: beta" in l).split()
    assert par_alpha[-3:] == _FROZEN_AGENT_CELLS, f"alpha row: {par_alpha!r}"
    assert par_beta[-3:] == _FROZEN_AGENT_CELLS, f"beta row: {par_beta!r}"


def test_real_session_time_columns_invariants(fake_home_with_real_session) -> None:
    """End-to-end over the REAL fixture, historical subprocess format (every
    assertion here is invariant to wall-clock `now`):

        work + wait == total   (±1s — flooring three values independently
                                can diverge by exactly one second at most)
        main: row == sum: row  (union consequence)
        all three values > 0   (a long real interactive session worked AND
                                idled at some point)
        the table renders the three time-column labels, and at least one
        agent row carries populated duration cells.
    """
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({"session_id": sid, "model": {"display_name": "X"}})
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0, (
        f"non-zero exit; stderr={result.stderr.decode('utf-8', 'replace')}"
    )
    lines = result.stdout.decode("utf-8").splitlines()
    assert lines[1].split() == [
        "|", "in", "out", "cached", "work", "wait", "total",
    ], f"labels: {lines[1]!r}"

    sum_cells = next(l for l in lines if l.startswith("| sum:")).split()
    main_cells = next(l for l in lines if l.startswith("| main:")).split()
    session_triple = sum_cells[-3:]
    for cell in session_triple:
        assert _is_duration_cell(cell), f"non-duration session cell: {cell!r}"
    work, wait_s, total_s = (_hms_seconds(c) for c in session_triple)

    assert abs((work + wait_s) - total_s) <= 1, (
        f"work({work}) + wait({wait_s}) != total({total_s}) beyond ±1s "
        f"(raw cells: {session_triple!r})"
    )
    assert session_triple == main_cells[-3:], (
        f"main row diverged from sum row: {main_cells[-3:]!r} vs {session_triple!r}"
    )
    assert work > 0, f"real session rendered zero work: {session_triple!r}"
    assert wait_s > 0, f"real session rendered zero wait: {session_triple!r}"
    assert total_s > 0, f"real session rendered zero total: {session_triple!r}"

    main_index = next(i for i, l in enumerate(lines) if l.startswith("| main:"))
    agent_rows = lines[main_index + 1:]
    assert agent_rows, "fixture must render at least one agent row"
    filled = sum(
        1
        for row in agent_rows
        if all(_is_duration_cell(c) for c in row.split()[-3:])
    )
    assert filled >= 1, (
        f"expected at least one agent row with duration cells:\n"
        + "\n".join(agent_rows[:5])
    )
