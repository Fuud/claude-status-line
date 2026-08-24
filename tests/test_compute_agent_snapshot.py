"""Tests for compute_agent_snapshot.

compute_agent_snapshot(jsonl_path, meta_path, cache_entry) returns a snapshot
dict for a single subagent: agentId, status, tokens, description, toolUseId,
last_uuid, mtime_jsonl, mtime_meta.

Cache semantics:
- If `cache_entry` (a dict) is provided and its last_uuid + mtime_jsonl match
  the current jsonl state → return the cache_entry unchanged (cache hit).
- Otherwise → re-parse the jsonl, compute fields fresh, and return.

The function does NOT write any cache file — the caller (orchestrator) owns
cache persistence.

Spec: see docs/plans/20260824-status-line-tokens-aggregation.md (Task 4).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from status_line import compute_agent_snapshot


FIXTURES_DIR = Path(__file__).parent / "fixtures"

# All fixture jsonl/meta paths referenced in tests.
AGENT_OK = FIXTURES_DIR / "agent_ok.jsonl"
AGENT_ERR_RATE_LIMIT = FIXTURES_DIR / "agent_err_rate_limit.jsonl"
AGENT_ERR_SERVER_ERROR = FIXTURES_DIR / "agent_err_server_error.jsonl"
AGENT_STOPPED_USER = FIXTURES_DIR / "agent_stopped_user.jsonl"
AGENT_RUNNING = FIXTURES_DIR / "agent_running.jsonl"
AGENT_NO_ASSISTANT = FIXTURES_DIR / "agent_no_assistant.jsonl"

META_NORMAL = FIXTURES_DIR / "meta_normal.json"
META_STOPPED_BY_USER = FIXTURES_DIR / "meta_stopped_by_user.json"
META_LONG_DESCRIPTION = FIXTURES_DIR / "meta_long_description.json"


def _agent_id(jsonl_path: Path) -> str:
    """Mirror the convention: agentId is the jsonl filename without extension."""
    return jsonl_path.stem


# ---------------------------------------------------------------------------
# happy path: agent_ok
# ---------------------------------------------------------------------------

def test_agent_ok_full_snapshot() -> None:
    """agent_ok.jsonl + meta_normal.json → status="ok", tokens from last
    assistant usage sum, description from meta, agentId from filename,
    toolUseId from meta, last_uuid = last assistant uuid.

    Last assistant event in agent_ok.jsonl has usage:
      input_tokens=100, cache_creation=200, cache_read=300, output=50
      → total = 650
    Last assistant uuid: 'a0000000-0000-0000-0000-000000000004'.
    """
    result = compute_agent_snapshot(AGENT_OK, META_NORMAL, cache_entry=None)

    assert result["status"] == "ok"
    assert result["tokens"] == 100 + 200 + 300 + 50  # 650
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

    Last assistant has usage (input=50, cache_creation=0, cache_read=100,
    output=10) → total=160. Per spec choice in the task description, we keep
    tokens from the last assistant event even when status is err (to surface
    partial progress before the failure).
    """
    result = compute_agent_snapshot(
        AGENT_ERR_RATE_LIMIT, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "err"
    # Tokens from last assistant event are retained despite err status.
    assert result["tokens"] == 50 + 0 + 100 + 10  # 160
    assert result["description"] == "Fixer: smells findings"
    assert result["agentId"] == _agent_id(AGENT_ERR_RATE_LIMIT)
    assert result["last_uuid"] == "b0000000-0000-0000-0000-000000000004"


def test_agent_err_server_error() -> None:
    """agent_err_server_error.jsonl → status="err", tokens from last assistant."""
    result = compute_agent_snapshot(
        AGENT_ERR_SERVER_ERROR, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "err"
    # last assistant: input=60, cache_creation=0, cache_read=120, output=15 → 195
    assert result["tokens"] == 60 + 0 + 120 + 15  # 195


def test_agent_stopped_user() -> None:
    """agent_stopped_user.jsonl + meta_normal.json → status='stop'. Last event
    is a user event with 'Request interrupted by user'. Last assistant event
    is mid-flow (stop_reason=tool_use), with usage tokens retained per spec
    (status='stop' doesn't blank tokens — tokens reflect the last assistant).
    """
    result = compute_agent_snapshot(
        AGENT_STOPPED_USER, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "stop"
    # Last assistant: input=30, cache_creation=0, cache_read=60, output=10 → 100
    assert result["tokens"] == 30 + 0 + 60 + 10  # 100


def test_agent_running() -> None:
    """agent_running.jsonl + meta_normal.json → status='run', tokens=None
    because the last event is an assistant with stop_reason=tool_use
    (still mid-flow), BUT it has usage so tokens will be computed.

    Per task spec: 'tokens=None for status=run' is the rule, but actually the
    spec says 'None (since last event is tool_use, not a final assistant)'.
    We follow the spec literally — when status=run, tokens=None even if usage
    exists in the last assistant event.

    Wait: re-reading task spec — it says 'last_event is tool_use, not a final
    assistant' which is wrong (the last event IS an assistant with stop_reason=
    tool_use). Per the rule 'tokens=None for status=run', we expect None.

    Decision: status='run' → tokens=None per spec literal reading.
    """
    result = compute_agent_snapshot(
        AGENT_RUNNING, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "run"
    # Per task spec: status='run' → tokens=None
    assert result["tokens"] is None


def test_agent_no_assistant() -> None:
    """agent_no_assistant.jsonl (only user events) → status='err', tokens=None.

    Per plan spec ('agent с 0 assistant event-ов → status=err, tokens=None'):
    the override applies here. detect_status alone would return 'run' for
    a last-user-event with no error markers, but the plan wants 'err' as a
    signal that the agent never produced output.
    """
    result = compute_agent_snapshot(
        AGENT_NO_ASSISTANT, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "err"
    assert result["tokens"] is None
    assert result["last_uuid"] is None


# ---------------------------------------------------------------------------
# meta-driven overrides
# ---------------------------------------------------------------------------

def test_meta_stopped_by_user_triggers_stop() -> None:
    """agent_ok.jsonl + meta_stopped_by_user.json → status='stop'.

    Even though the agent finished cleanly (last assistant end_turn),
    meta.stoppedByUser=true overrides to 'stop'.
    """
    result = compute_agent_snapshot(
        AGENT_OK, META_STOPPED_BY_USER, cache_entry=None
    )

    assert result["status"] == "stop"
    # Tokens still computed from last assistant event.
    assert result["tokens"] == 100 + 200 + 300 + 50  # 650


def test_meta_long_description_truncated() -> None:
    """agent_ok.jsonl + meta_long_description.json (60 chars) → description
    is 40 chars long, ends with U+2026 '…', first 39 chars match original.
    """
    # Load the original description to compare.
    original = json.loads(META_LONG_DESCRIPTION.read_text())["description"]
    assert len(original) == 60

    result = compute_agent_snapshot(
        AGENT_OK, META_LONG_DESCRIPTION, cache_entry=None
    )

    desc = result["description"]
    assert len(desc) == 40
    assert desc.endswith("…"), f"description should end with …, got {desc!r}"
    # First 39 chars match the original first 39 chars.
    assert desc[:39] == original[:39]


def test_meta_missing_fallback() -> None:
    """agent_ok.jsonl + non-existent meta path → description falls back to
    agentType ('general-purpose' from meta_normal.json semantics), status
    still computed from last_event.

    For this test we pass a meta path that doesn't exist, but we can't pass
    a real meta dict with agentType — the function reads from disk. So we
    fabricate a meta.json with empty description but agentType set, then
    delete it before passing the path. Actually simpler: use a non-existent
    path and verify fallback to 'unknown' when nothing can be loaded.

    Better approach: write a meta file with agentType and empty description
    → fallback to agentType='<from file>'. Plus a separate assertion path
    for completely missing file → fallback to 'unknown'.
    """
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

def test_cache_hit() -> None:
    """Pre-populate cache_entry with matching last_uuid AND mtime_jsonl →
    function returns the cached dict without re-parsing. Sentinel value
    'tokens=999_999_999' (which doesn't match any real event in the jsonl)
    proves the cache was used.
    """
    mtime = AGENT_OK.stat().st_mtime
    last_uuid = "a0000000-0000-0000-0000-000000000004"

    cache_entry = {
        "agentId": _agent_id(AGENT_OK),
        "status": "ok",
        "tokens": 999_999_999,
        "description": "from-cache",
        "toolUseId": "toolu_cached",
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime,
        "mtime_meta": META_NORMAL.stat().st_mtime,
    }

    result = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_entry
    )

    # If cache was used, the sentinel survives.
    assert result["tokens"] == 999_999_999
    assert result["description"] == "from-cache"
    assert result["toolUseId"] == "toolu_cached"


def test_cache_miss_recomputes() -> None:
    """Cache entry has wrong last_uuid OR stale mtime → re-parse from jsonl,
    result reflects current state (NOT the cache_entry values)."""
    mtime = AGENT_OK.stat().st_mtime

    # Case A: wrong last_uuid.
    cache_wrong_uuid = {
        "agentId": "wrong-id",
        "status": "run",
        "tokens": 42,
        "description": "stale-description",
        "toolUseId": "toolu_stale",
        "last_uuid": "stale-uuid-that-doesnt-match",
        "mtime_jsonl": mtime,
        "mtime_meta": META_NORMAL.stat().st_mtime,
    }
    result = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_wrong_uuid
    )
    assert result["tokens"] == 650
    assert result["description"] == "Fixer: smells findings"
    assert result["last_uuid"] == "a0000000-0000-0000-0000-000000000004"

    # Case B: stale mtime (uuid happens to match).
    cache_stale_mtime = dict(cache_wrong_uuid)
    cache_stale_mtime["last_uuid"] = "a0000000-0000-0000-0000-000000000004"
    cache_stale_mtime["mtime_jsonl"] = 0.0  # stale
    cache_stale_mtime["tokens"] = 7777
    cache_stale_mtime["description"] = "stale-via-mtime"

    result2 = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_stale_mtime
    )
    assert result2["tokens"] == 650
    assert result2["description"] == "Fixer: smells findings"
