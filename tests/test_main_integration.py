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

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REAL_SESSION_SID = "f5044e4f-3e01-4330-be72-eb008a1d035e"
ENCODED_PROJECT = "C--Users-f-bobin-IdeaProjects-agentic-terminal"

STATUS_LINE_PY = Path(__file__).parent.parent / "status_line.py"


def _run_main(
    stdin: str, home: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Spawn status_line.py with HOME=<home> and feed stdin (str).

    CLAUDE_CODE_CONTEXT_LIMIT is always popped so context-limit resolution
    is deterministic; pass an override via extra_env."""
    env = os.environ.copy()
    # Path.home() on Windows Python consults USERPROFILE first, then HOME.
    # Override both so the child process sees a deterministic home.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    env.pop("CLAUDE_CODE_CONTEXT_LIMIT", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(STATUS_LINE_PY)],
        input=stdin.encode("utf-8"),
        capture_output=True,
        timeout=30,
        env=env,
    )


@pytest.fixture
def fake_home_with_real_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a fake $HOME with .claude/projects/<encoded>/<sid> pointing
    at the real_session fixture, and a writable .claude/status_line/data/.

    Returns (tmp_path, session_id).
    """
    real_session_root = FIXTURES / "real_session"
    real_session_dir = real_session_root / REAL_SESSION_SID
    assert real_session_root.exists(), "fixtures/real_session missing"
    assert real_session_dir.exists(), f"real session dir missing: {real_session_dir}"

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
    # [deviation] Task 2 dropped the `total` field from compute_main_cum's
    # result and from the persisted cache. The breakdown-table refactor
    # passes cum_in/cum_out/cum_cache_read directly to render; `total` is
    # dead. Verify the new breakdown keys exist and reflect real usage.
    assert "cum_in" in loaded
    assert "cum_out" in loaded
    assert "cum_cache_read" in loaded
    assert "total" not in loaded, (
        f"`total` must not appear in the persisted cache after Task 2, "
        f"got keys: {sorted(loaded.keys())}"
    )
    assert loaded["cum_in"] > 0
    assert loaded["cum_out"] > 0


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
    # Output must be byte-identical to the first call (no flake — the cache
    # hit should be deterministic for unchanged files).
    assert second_output == first_output, (
        "cache-hit output diverged from cache-miss output:\n"
        f"first:  {first_output[:200]!r}\n"
        f"second: {second_output[:200]!r}"
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
#    → 1M, else 200k.
# ---------------------------------------------------------------------------

# Payload context value used across these tests: 15500 tokens.
# Against 200k → "16K (8%)" (round(15.5)=16, round(7.75)=8).
# Against 1M   → "16K (2%)"  (round(1.55)=2).
# Against 500k → "16K (3%)"  (round(3.1)=3).
CTX_TOKENS = 15_500


def test_header_context_from_payload(fake_home_with_real_session) -> None:
    """Payload carries context_window.total_input_tokens → header shows it
    after User, percent vs the 200k default (plain model, no env)."""
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
    # A distinct payload value (30000 → "30K (15%)" vs 200k) that the jsonl
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
# Expected aggregates: cum_in=1800 → "2k", cum_out=150 → "150",
# cum_cache_read=4000 → "4k"; last-event occupancy 800+0+1000=1800 → "2K (1%)".
_DIRLESS_EXPECTED_CELLS = ["2k", "150", "4k"]
# First event breakdown (the "start:" row): in=1000 → "1k", out=100 → "100",
# cache_read=3000 → "3k".
_DIRLESS_EXPECTED_START_CELLS = ["1k", "100", "3k"]


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
    # "| " table-row prefix, then the three labels
    assert labels.split() == ["|", "in", "out", "cached"], f"labels: {labels!r}"
    # start row carries the FIRST event's breakdown.
    start_cells = start.split()
    assert start_cells[:2] == ["|", "start:"], f"start row: {start!r}"
    assert start_cells[2:] == _DIRLESS_EXPECTED_START_CELLS, f"start row: {start!r}"
    cells = main.split()
    assert cells[:2] == ["|", "main:"], f"main row: {main!r}"
    assert cells[2:] == _DIRLESS_EXPECTED_CELLS, f"main row: {main!r}"
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
    assert lines[3].split()[2:] == _DIRLESS_EXPECTED_CELLS, f"main: {lines[3]!r}"


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
    assert second.stdout == first.stdout, (
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
