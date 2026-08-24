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
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REAL_SESSION_SID = "f5044e4f-3e01-4330-be72-eb008a1d035e"
ENCODED_PROJECT = "C--Users-f-bobin-IdeaProjects-agentic-terminal"

STATUS_LINE_PY = Path(__file__).parent.parent / "status_line.py"


def _run_main(stdin: str, home: Path) -> subprocess.CompletedProcess:
    """Spawn status_line.py with HOME=<home> and feed stdin (str)."""
    env = os.environ.copy()
    # Path.home() on Windows Python consults USERPROFILE first, then HOME.
    # Override both so the child process sees a deterministic home.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
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
    assert real_session_root.exists(), f"fixtures/real_session missing"
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
# 1. Real session → 41 lines (header + sum + main + 38 agents)
# ---------------------------------------------------------------------------

def test_real_session_38_agents(fake_home_with_real_session) -> None:
    """Feed the real session through main(); expect 41 lines, presence of
    [ok]/[err]/[stop] tags, and Task 1 as the first agent line."""
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
    # header + sum + main + 38 agents = 41
    assert len(lines) == 41, (
        f"expected 41 lines, got {len(lines)}; first 5: {lines[:5]}; "
        f"stderr: {result.stderr.decode('utf-8', 'replace')}"
    )
    # All three status tags must appear (real session covers ok/err/stop).
    # [deviation] The f5044e4f session evolved after the plan was written —
    # when the fixture was copied the session had no agents with
    # stoppedByUser=true in meta, so [stop] is not currently present in
    # the snapshot. We only assert [ok] and [err] here. If a future session
    # snapshot has stopped agents, add the [stop] assertion back.
    assert "[ok]" in output, "expected at least one [ok] in output"
    assert "[err]" in output, "expected at least one [err] in output"
    # Header / sum / main lines have predictable prefixes
    assert lines[0].startswith("Session:"), f"line 0: {lines[0]!r}"
    assert lines[1].startswith("sum:"), f"line 1: {lines[1]!r}"
    assert lines[2].startswith("main:"), f"line 2: {lines[2]!r}"
    # All agent lines start with a bracketed status tag
    for line in lines[3:]:
        assert line.startswith("["), f"agent line missing status tag: {line!r}"
    # The first agent in the output should be the one with the LOWEST
    # tool_use position in main jsonl. In the f5044e4f session that's
    # "Review implementation plan" (toolUseId=Agent_61, position 260 in
    # the main jsonl) — not Task 1 (Agent_103, position 431). The plan's
    # assertion "Task 1 first" assumed Task 1 was at position 0; the real
    # session has Agent_61/62/... before Agent_103. We check for the actual
    # first-sorted agent instead.
    first_agent = lines[3]
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
    what the fixture actually contains.
    """
    tmp_path, sid = fake_home_with_real_session
    stdin = json.dumps({
        "session_id": sid,
        "model": {"display_name": "X"},
    })
    result = _run_main(stdin, tmp_path)
    assert result.returncode == 0
    output = result.stdout.decode("utf-8")
    # The current f5044e4f fixture has exactly 1 [err] agent (Review:
    # quality) at the time of this test. If the fixture is regenerated,
    # update this count to match — but the loose ">= 1" assertion would
    # silently accept regressions in the count.
    assert output.count("[err]") == 1
    assert output.count("[ok]") >= 1


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
    assert "total" in loaded
    assert loaded["total"] > 0


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