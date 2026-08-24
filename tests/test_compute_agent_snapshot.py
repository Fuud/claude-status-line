"""Tests for compute_agent_snapshot.

compute_agent_snapshot(jsonl_path, meta_path, cache_entry) returns a snapshot
dict for a single subagent: agentId, status, tokens_in, tokens_out,
tokens_cached, description, toolUseId, last_uuid, mtime_jsonl, mtime_meta.

Cache semantics:
- If `cache_entry` (a dict) is provided and its last_uuid + mtime_jsonl +
  mtime_meta all match the current file state AND breakdown fields
  (tokens_in/out/cached) are present in cache_entry → return the cache_entry
  unchanged (cache hit). The field-presence check guards against stale
  caches from the pre-breakdown schema.
- Otherwise → re-parse the jsonl, compute fields fresh, and return.

The function does NOT write any cache file — the caller (orchestrator) owns
cache persistence.

Spec: see docs/plans/20260824-token-breakdown-table.md (Task 1).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from status_line import compute_agent_snapshot, _compute_agents


FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Fixture jsonl paths used across multiple tests.
AGENT_OK = FIXTURES_DIR / "agent_ok.jsonl"
AGENT_ERR_RATE_LIMIT = FIXTURES_DIR / "agent_err_rate_limit.jsonl"
AGENT_STOPPED_USER = FIXTURES_DIR / "agent_stopped_user.jsonl"
AGENT_RUNNING = FIXTURES_DIR / "agent_running.jsonl"
AGENT_KILLED = FIXTURES_DIR / "agent-killed-in-tool-use.jsonl"
AGENT_KILLED_META = FIXTURES_DIR / "agent-killed-in-tool-use.meta.json"
AGENT_COMPLETED = FIXTURES_DIR / "agent-completed-after-tool-use.jsonl"
AGENT_COMPLETED_META = FIXTURES_DIR / "agent-completed-after-tool-use.meta.json"
AGENT_ERR_API = FIXTURES_DIR / "agent-err-in-tool-use.jsonl"
AGENT_ERR_API_META = FIXTURES_DIR / "agent-err-in-tool-use.meta.json"

META_NORMAL = FIXTURES_DIR / "meta_normal.json"
META_STOPPED_BY_USER = FIXTURES_DIR / "meta_stopped_by_user.json"


def _agent_id(jsonl_path: Path) -> str:
    """Mirror the convention: agentId is the jsonl filename without extension."""
    return jsonl_path.stem


# ---------------------------------------------------------------------------
# happy path: agent_ok
# ---------------------------------------------------------------------------

def test_agent_ok_full_snapshot() -> None:
    """agent_ok.jsonl + meta_normal.json → status="ok", breakdown from last
    assistant usage (in=100, out=50, cached=300; cache_creation=200 is dropped),
    description from meta, agentId from filename, toolUseId from meta,
    last_uuid = last assistant uuid.

    No `tokens` field — replaced by three breakdown fields per plan Task 1.
    """
    result = compute_agent_snapshot(AGENT_OK, META_NORMAL, cache_entry=None)

    assert result["status"] == "ok"
    # Last assistant: input=100, cache_read=300, output=50; cache_creation NOT
    # in any column.
    assert result["tokens_in"] == 100
    assert result["tokens_out"] == 50
    assert result["tokens_cached"] == 300
    # Field `tokens` no longer exists.
    assert "tokens" not in result
    assert result["description"] == "Fixer: smells findings"
    assert result["agentId"] == _agent_id(AGENT_OK)
    assert result["toolUseId"] == "toolu_001"
    assert result["last_uuid"] == "a0000000-0000-0000-0000-000000000004"
    assert isinstance(result["mtime_jsonl"], float)
    assert isinstance(result["mtime_meta"], float)
    # mtime_jsonl should be > 0 since file exists
    assert result["mtime_jsonl"] > 0
    assert result["mtime_meta"] > 0


# ---------------------------------------------------------------------------
# error agents
# ---------------------------------------------------------------------------

def test_agent_err_rate_limit() -> None:
    """agent_err_rate_limit.jsonl + meta_normal.json → status="err".

    Last assistant: input=50, cache_read=100, output=10 (cache_creation=0).
    Breakdown values retained even on err status.
    """
    result = compute_agent_snapshot(
        AGENT_ERR_RATE_LIMIT, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "err"
    assert result["tokens_in"] == 50
    assert result["tokens_out"] == 10
    assert result["tokens_cached"] == 100
    assert result["description"] == "Fixer: smells findings"
    assert result["agentId"] == _agent_id(AGENT_ERR_RATE_LIMIT)
    assert result["last_uuid"] == "b0000000-0000-0000-0000-000000000004"


def test_agent_err_server_error() -> None:
    """agent_err_server_error.jsonl → status="err", breakdown from last assistant.
    Last assistant: input=60, cache_read=120, output=15.
    """
    result = compute_agent_snapshot(
        FIXTURES_DIR / "agent_err_server_error.jsonl",
        META_NORMAL,
        cache_entry=None,
    )

    assert result["status"] == "err"
    assert result["tokens_in"] == 60
    assert result["tokens_out"] == 15
    assert result["tokens_cached"] == 120


def test_agent_stopped_user() -> None:
    """agent_stopped_user.jsonl + meta_normal.json → status='stop'. Last event
    is a user event with 'Request interrupted by user'. Last assistant event
    is mid-flow (stop_reason=tool_use), with breakdown retained per plan.
    Last assistant: input=30, cache_read=60, output=10.
    """
    result = compute_agent_snapshot(
        AGENT_STOPPED_USER, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "stop"
    assert result["tokens_in"] == 30
    assert result["tokens_out"] == 10
    assert result["tokens_cached"] == 60


def test_agent_running() -> None:
    """agent_running.jsonl + meta_normal.json → status='run', breakdown
    from last assistant event is non-zero (no longer blanked by run status).

    Previously `tokens=None` for run; after Task 1, breakdown fields are
    always populated from the last assistant event's usage. The user sees
    current values, not blanks.
    """
    result = compute_agent_snapshot(
        AGENT_RUNNING, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "run"
    # Last assistant: input=40, cache_read=80, output=15.
    assert result["tokens_in"] == 40
    assert result["tokens_out"] == 15
    assert result["tokens_cached"] == 80
    # No `tokens` field anywhere.
    assert "tokens" not in result


def test_agent_no_assistant() -> None:
    """agent_no_assistant.jsonl (only user events) → status='err',
    tokens_in/out/cached all zero.

    Per plan spec, the agent with zero assistant events surfaces breakdown
    zeros so the row is never blanked. Status override → "err" is preserved.
    """
    result = compute_agent_snapshot(
        FIXTURES_DIR / "agent_no_assistant.jsonl",
        META_NORMAL,
        cache_entry=None,
    )

    assert result["status"] == "err"
    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert result["tokens_cached"] == 0
    assert result["last_uuid"] is None
    assert "tokens" not in result


# ---------------------------------------------------------------------------
# breakdown-field edge cases (new per Task 1)
# ---------------------------------------------------------------------------

def test_breakdown_absent_usage_block_yields_zeros(tmp_path: Path) -> None:
    """Assistant event present but `message.usage` absent → all three
    breakdown fields = 0. Synthesize a degenerate assistant event with
    no usage key.
    """
    jsonl = tmp_path / "agent-no-usage.jsonl"
    jsonl.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg-no-usage",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "model": "claude-opus-4-1",
                "stop_reason": "end_turn",
            },
            "uuid": "abcd0000-0000-0000-0000-000000000001",
        })
        + "\n"
    )

    result = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert result["status"] == "ok"
    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert result["tokens_cached"] == 0


def test_breakdown_zero_when_no_assistant_events(tmp_path: Path) -> None:
    """Empty jsonl (no events at all) → last_event is None → all three
    fields = 0. Distinct from `agent_no_assistant.jsonl` (which has user
    events) — this covers the truly-empty case.
    """
    jsonl = tmp_path / "agent-empty.jsonl"
    jsonl.write_text("")  # zero lines

    result = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert result["tokens_cached"] == 0
    assert result["last_uuid"] is None


def test_breakdown_missing_jsonl_yields_zeros(tmp_path: Path) -> None:
    """Missing jsonl file → no events → all three fields = 0."""
    missing = tmp_path / "agent-does-not-exist.jsonl"
    assert not missing.exists()

    result = compute_agent_snapshot(missing, META_NORMAL, cache_entry=None)

    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert result["tokens_cached"] == 0
    assert result["last_uuid"] is None


# ---------------------------------------------------------------------------
# meta-driven overrides
# ---------------------------------------------------------------------------

def test_meta_stopped_by_user_triggers_stop() -> None:
    """agent_ok.jsonl + meta_stopped_by_user.json → status='stop'.

    Even though the agent finished cleanly (last assistant end_turn),
    meta.stoppedByUser=true overrides to 'stop'. Breakdown still computed.
    """
    result = compute_agent_snapshot(
        AGENT_OK, META_STOPPED_BY_USER, cache_entry=None
    )

    assert result["status"] == "stop"
    assert result["tokens_in"] == 100
    assert result["tokens_out"] == 50
    assert result["tokens_cached"] == 300


def test_meta_long_description_truncated() -> None:
    """agent_ok.jsonl + meta_long_description.json (60 chars) → description
    is 40 chars long, ends with U+2026 '…', first 39 chars match original.
    """
    meta_path = FIXTURES_DIR / "meta_long_description.json"
    original = json.loads(meta_path.read_text())["description"]
    assert len(original) == 60

    result = compute_agent_snapshot(AGENT_OK, meta_path, cache_entry=None)

    desc = result["description"]
    assert len(desc) == 40
    assert desc.endswith("…"), f"description should end with …, got {desc!r}"
    # First 39 chars match the original first 39 chars.
    assert desc[:39] == original[:39]


def test_meta_missing_fallback() -> None:
    """Missing meta or empty description → fallback to agentType, then 'unknown'."""
    # Case 1: meta file with empty description but valid agentType.
    meta_with_type = FIXTURES_DIR / "_meta_empty_desc.json"
    meta_with_type.write_text(json.dumps({
        "agentType": "Explore",
        "toolUseId": "toolu_999",
    }))
    try:
        result = compute_agent_snapshot(
            AGENT_OK, meta_with_type, cache_entry=None
        )
        assert result["description"] == "Explore"
        assert result["toolUseId"] == "toolu_999"
        # Status still computed from last event.
        assert result["status"] == "ok"
    finally:
        meta_with_type.unlink(missing_ok=True)

    # Case 2: completely missing meta path → fallback to 'unknown'.
    missing_meta = FIXTURES_DIR / "_meta_definitely_missing.json"
    if missing_meta.exists():
        missing_meta.unlink()

    result = compute_agent_snapshot(
        AGENT_OK, missing_meta, cache_entry=None
    )
    assert result["description"] == "unknown"
    assert result["status"] == "ok"
    # mtime_meta = 0 for missing file per spec.
    assert result["mtime_meta"] == 0


# ---------------------------------------------------------------------------
# cache hit / miss
# ---------------------------------------------------------------------------

def test_cache_hit_with_breakdown_fields() -> None:
    """Pre-populate cache_entry with matching last_uuid + mtime_jsonl +
    mtime_meta AND all three breakdown fields → function returns the
    cached dict without re-parsing. Sentinel values on the breakdown
    fields prove the cache was used.
    """
    mtime = AGENT_OK.stat().st_mtime
    last_uuid = "a0000000-0000-0000-0000-000000000004"

    cache_entry = {
        "agentId": _agent_id(AGENT_OK),
        "status": "ok",
        "tokens_in": 11_111,
        "tokens_out": 22_222,
        "tokens_cached": 33_333,
        "description": "from-cache",
        "toolUseId": "toolu_cached",
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime,
        "mtime_meta": META_NORMAL.stat().st_mtime,
    }

    result = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_entry
    )

    # If cache was used, the sentinels survive.
    assert result["tokens_in"] == 11_111
    assert result["tokens_out"] == 22_222
    assert result["tokens_cached"] == 33_333
    assert result["description"] == "from-cache"
    assert result["toolUseId"] == "toolu_cached"


def test_cache_miss_when_breakdown_fields_missing() -> None:
    """[upgrade path] Cache entry has matching last_uuid + mtime_jsonl +
    mtime_meta but is missing one or more breakdown fields → cache MISS,
    forward re-parse fills in fresh values. Without this check, a stale
    pre-upgrade cache would render zeros (because render would fall back
    via `int(a.get(field) or 0)`) until the next jsonl mutation.
    """
    mtime = AGENT_OK.stat().st_mtime
    last_uuid = "a0000000-0000-0000-0000-000000000004"
    mtime_meta = META_NORMAL.stat().st_mtime

    # Case A: missing tokens_in entirely.
    cache_no_in = {
        "agentId": "wrong-id",
        "status": "run",
        "tokens_out": 222,
        "tokens_cached": 333,
        "description": "stale-no-in",
        "toolUseId": "toolu_stale",
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime,
        "mtime_meta": mtime_meta,
    }
    r_a = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_no_in
    )
    assert r_a["tokens_in"] == 100, (
        "cache miss expected: missing tokens_in triggers re-parse"
    )
    assert r_a["tokens_out"] == 50
    assert r_a["tokens_cached"] == 300
    assert r_a["description"] == "Fixer: smells findings"

    # Case B: missing tokens_out entirely.
    cache_no_out = dict(cache_no_in)
    cache_no_out["tokens_in"] = 111
    del cache_no_out["tokens_out"]
    r_b = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_no_out
    )
    assert r_b["tokens_out"] == 50, (
        "cache miss expected: missing tokens_out triggers re-parse"
    )
    assert r_b["tokens_in"] == 100
    assert r_b["tokens_cached"] == 300

    # Case C: missing tokens_cached entirely.
    cache_no_cached = dict(cache_no_in)
    cache_no_cached["tokens_in"] = 111
    cache_no_cached["tokens_out"] = 222
    del cache_no_cached["tokens_cached"]
    r_c = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_no_cached
    )
    assert r_c["tokens_cached"] == 300, (
        "cache miss expected: missing tokens_cached triggers re-parse"
    )
    assert r_c["tokens_in"] == 100
    assert r_c["tokens_out"] == 50


def test_cache_miss_recomputes() -> None:
    """Cache entry has wrong last_uuid OR stale mtime → re-parse from jsonl,
    result reflects current state (NOT the cache_entry values)."""
    mtime = AGENT_OK.stat().st_mtime

    # Case A: wrong last_uuid.
    cache_wrong_uuid = {
        "agentId": "wrong-id",
        "status": "run",
        "tokens_in": 111,
        "tokens_out": 222,
        "tokens_cached": 333,
        "description": "stale-description",
        "toolUseId": "toolu_stale",
        "last_uuid": "stale-uuid-that-doesnt-match",
        "mtime_jsonl": mtime,
        "mtime_meta": META_NORMAL.stat().st_mtime,
    }
    result = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_wrong_uuid
    )
    assert result["tokens_in"] == 100
    assert result["tokens_out"] == 50
    assert result["tokens_cached"] == 300
    assert result["description"] == "Fixer: smells findings"
    assert result["last_uuid"] == "a0000000-0000-0000-0000-000000000004"

    # Case B: stale mtime (uuid happens to match).
    cache_stale_mtime = dict(cache_wrong_uuid)
    cache_stale_mtime["last_uuid"] = "a0000000-0000-0000-0000-000000000004"
    cache_stale_mtime["mtime_jsonl"] = 0.0  # stale
    cache_stale_mtime["tokens_in"] = 7777
    cache_stale_mtime["tokens_out"] = 7778
    cache_stale_mtime["tokens_cached"] = 7779
    cache_stale_mtime["description"] = "stale-via-mtime"

    result2 = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_stale_mtime
    )
    assert result2["tokens_in"] == 100
    assert result2["tokens_out"] == 50
    assert result2["tokens_cached"] == 300
    assert result2["description"] == "Fixer: smells findings"


def test_cache_miss_on_meta_mtime_change(tmp_path: Path) -> None:
    """[deviation] Cache key includes mtime_meta so a meta.json edit (e.g.
    stoppedByUser added later, description updated) invalidates the cache
    even when jsonl mtime+uuid are unchanged.

    Strategy: copy AGENT_OK to tmp, copy META_NORMAL to tmp with a different
    filename, build cache_entry with matching last_uuid+mtime_jsonl AND
    breakdown fields, but STALE mtime_meta, then mutate the meta file
    (touch with new mtime) and verify the cache is invalidated."""
    src_jsonl = AGENT_OK
    src_meta = META_NORMAL
    work_jsonl = tmp_path / "agent-test.jsonl"
    work_meta = tmp_path / "agent-test.meta.json"
    work_jsonl.write_bytes(src_jsonl.read_bytes())
    work_meta.write_bytes(src_meta.read_bytes())

    # Ensure mtime_meta is recorded at a slightly earlier time so we can
    # bump it forward via touch without clock-skew flakes.
    mtime_jsonl = work_jsonl.stat().st_mtime
    mtime_meta_v1 = work_meta.stat().st_mtime
    last_uuid = "a0000000-0000-0000-0000-000000000004"

    # Cache built when meta was at v1. Includes the three breakdown fields
    # so the only thing invalidating the cache is mtime_meta.
    cache_v1 = {
        "agentId": "agent-test",
        "status": "ok",
        "tokens_in": 9_999_999,  # sentinel — should not survive
        "tokens_out": 9_999_999,
        "tokens_cached": 9_999_999,
        "description": "stale-from-v1",
        "toolUseId": "toolu_v1",
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime_jsonl,
        "mtime_meta": mtime_meta_v1,
    }

    # First call: cache hits (matching key + breakdown present).
    r1 = compute_agent_snapshot(work_jsonl, work_meta, cache_entry=cache_v1)
    assert r1["tokens_in"] == 9_999_999, "expected cache hit on first call"
    assert r1["tokens_out"] == 9_999_999
    assert r1["tokens_cached"] == 9_999_999

    # Mutate the meta file — write new content AND bump mtime forward.
    work_meta.write_text(json.dumps({
        "description": "edited description",
        "agentType": "general-purpose",
        "toolUseId": "toolu_v2",
    }))
    # Force a clearly newer mtime (1.5s in the future to dodge FS resolution).
    future_mtime = mtime_meta_v1 + 5.0
    os.utime(work_meta, (future_mtime, future_mtime))

    # Second call: cache should miss because mtime_meta changed.
    r2 = compute_agent_snapshot(work_jsonl, work_meta, cache_entry=cache_v1)
    assert r2["tokens_in"] != 9_999_999, (
        "cache should have invalidated on meta mtime change, but the "
        "sentinel survived"
    )
    assert r2["description"] == "edited description"
    assert r2["toolUseId"] == "toolu_v2"


# ---------------------------------------------------------------------------
# _compute_agents orchestrator override (added per 20260824-subagent-status-via-queue-notifications)
# ---------------------------------------------------------------------------

def _make_session_with_agent(
    tmp_path: Path, src_jsonl: Path, src_meta: Path
) -> tuple[Path, Path, str]:
    """Create session_dir/subagents/agent-XXX.{jsonl,meta.json} by copying the
    given fixture pair. The destination filename is always `agent-test`
    (matches the production `agent-*.jsonl` glob); contents come from
    `src_jsonl`/`src_meta`.

    Returns (session_dir, agents_cache_path, agent_id) where agent_id is the
    canonical stem ("agent-test") — callers use it to derive the join key.
    """
    session_dir = tmp_path / "session-abc"
    subagents_dir = session_dir / "subagents"
    subagents_dir.mkdir(parents=True)
    agent_id = "agent-test"
    dst_jsonl = subagents_dir / f"{agent_id}.jsonl"
    dst_meta = subagents_dir / f"{agent_id}.meta.json"
    dst_jsonl.write_bytes(src_jsonl.read_bytes())
    dst_meta.write_bytes(src_meta.read_bytes())
    agents_cache = tmp_path / "agents_cache.json"
    return session_dir, agents_cache, agent_id


def test_compute_agents_no_task_notifications_backwards_compat(
    tmp_path: Path,
) -> None:
    """Empty task_notifications dict → _compute_agents behaves as before.
    Pre-existing behavior for agents without queue signal must be preserved."""
    session_dir, agents_cache, _ = _make_session_with_agent(
        tmp_path, AGENT_RUNNING, META_NORMAL
    )

    agents = _compute_agents(session_dir, agents_cache, task_notifications={})

    assert len(agents) == 1
    # agent_running.jsonl ends with tool_use (no end_turn) → status="run"
    assert agents[0]["status"] == "run"


def test_compute_agents_killed_in_tool_use_with_queue_signal(
    tmp_path: Path,
) -> None:
    """Agent jsonl ends with tool_use (mid-flight); queue-notification says
    killed → orchestrator overrides to 'kill'."""
    session_dir, agents_cache, agent_id = _make_session_with_agent(
        tmp_path, AGENT_KILLED, AGENT_KILLED_META
    )
    task_key = agent_id[len("agent-"):]  # "test" (canonical stem-minus-prefix)

    agents = _compute_agents(
        session_dir, agents_cache, task_notifications={task_key: "kill"}
    )

    assert len(agents) == 1
    assert agents[0]["status"] == "kill"


def test_compute_agents_completed_after_tool_use_with_queue_signal(
    tmp_path: Path,
) -> None:
    """Agent jsonl ends with end_turn; queue-notification says completed.
    Orchestrator overrides 'ok' → 'ok' (no change — queue signal is consistent
    with clean end_turn)."""
    session_dir, agents_cache, agent_id = _make_session_with_agent(
        tmp_path, AGENT_COMPLETED, AGENT_COMPLETED_META
    )
    task_key = agent_id[len("agent-"):]

    agents = _compute_agents(
        session_dir, agents_cache, task_notifications={task_key: "ok"}
    )

    assert len(agents) == 1
    # Compute_agent_snapshot returns "ok" (end_turn); queue says "ok" — same.
    assert agents[0]["status"] == "ok"


def test_compute_agents_api_error_with_queue_signal_guard(
    tmp_path: Path,
) -> None:
    """[guard] Agent jsonl ends with assistant event with apiErrorStatus=429
    → compute_agent_snapshot returns 'err'. Queue-notification says
    'completed'. The guard 'status not in (err, stop)' MUST prevent the
    override from downgrading 'err' to 'ok'."""
    session_dir, agents_cache, agent_id = _make_session_with_agent(
        tmp_path, AGENT_ERR_API, AGENT_ERR_API_META
    )
    task_key = agent_id[len("agent-"):]

    agents = _compute_agents(
        session_dir, agents_cache, task_notifications={task_key: "ok"}
    )

    assert len(agents) == 1
    assert agents[0]["status"] == "err", (
        f"err must be preserved (guard); got {agents[0]['status']!r}"
    )


def test_compute_agents_stopped_by_user_with_queue_signal_guard(
    tmp_path: Path,
) -> None:
    """[guard] Agent meta.stoppedByUser=true → compute_agent_snapshot returns
    'stop'. Queue-notification says 'completed'. Guard must prevent the
    override from downgrading 'stop' to 'ok'."""
    session_dir, agents_cache, agent_id = _make_session_with_agent(
        tmp_path, AGENT_OK, META_STOPPED_BY_USER
    )
    task_key = agent_id[len("agent-"):]

    agents = _compute_agents(
        session_dir, agents_cache, task_notifications={task_key: "ok"}
    )

    assert len(agents) == 1
    assert agents[0]["status"] == "stop", (
        f"stop must be preserved (guard); got {agents[0]['status']!r}"
    )


def test_compute_agents_prefix_strip_join(tmp_path: Path) -> None:
    """The queue notification key is the agent's filename stem WITHOUT the
    'agent-' prefix. The orchestrator strips the prefix when looking up
    agents. Verifies with a non-prefixed task key in the dict."""
    session_dir, agents_cache, agent_id = _make_session_with_agent(
        tmp_path, AGENT_KILLED, AGENT_KILLED_META
    )
    task_key = agent_id[len("agent-"):]  # "test"

    agents = _compute_agents(
        session_dir, agents_cache, task_notifications={task_key: "kill"}
    )

    assert len(agents) == 1
    assert agents[0]["status"] == "kill"


def test_compute_agents_no_match_no_override(tmp_path: Path) -> None:
    """Queue-notification present but with a task-id NOT matching any agent →
    orchestrator ignores it. Agent status reflects compute_agent_snapshot
    alone."""
    session_dir, agents_cache, _ = _make_session_with_agent(
        tmp_path, AGENT_RUNNING, META_NORMAL
    )

    agents = _compute_agents(
        session_dir,
        agents_cache,
        task_notifications={"some-other-agent": "kill"},
    )

    assert len(agents) == 1
    # agent_running.jsonl → "run"; unmatched queue key has no effect.
    assert agents[0]["status"] == "run"
