"""Tests for compute_agent_snapshot.

compute_agent_snapshot(jsonl_path, meta_path, cache_entry) returns a snapshot
dict for a single subagent: agentId, status, tokens_in, tokens_out,
tokens_cached, models, description, toolUseId, last_uuid, mtime_jsonl,
mtime_meta, plus the time-segmentation fields ts_first, ts_last, qa_pauses,
qa_open_ts (plan 20260827-status-line-time-columns, Task 4).

Token semantics (20260826-status-line-model-cost-columns, Task 3):
- tokens_in/tokens_out/tokens_cached are CUMULATIVE sums over ALL assistant
  events with a usage block — not the last event's usage (agreed behavior
  change; the old last-event semantics were the pre-model-columns schema).
- models is the per-model breakdown {model_id: {"in","out","cached"}}
  accumulated over the same events ({} when no assistant event has usage).

Cache semantics:
- If `cache_entry` (a dict) is provided and its last_uuid + mtime_jsonl +
  mtime_meta all match the current file state AND breakdown fields
  (tokens_in/out/cached) AND `models` AND the four time-segmentation
  fields (ts_first/ts_last/qa_pauses/qa_open_ts) are present in
  cache_entry AND its status_rev equals the current _STATUS_REV →
  return the cache_entry unchanged (cache hit). The field-presence
  checks guard against stale caches from pre-upgrade schemas (including
  pre-time-column ones); the status_rev check guards against pre-rev
  STATUS LOGIC — a cached "run" from before an _is_assistant_error fix
  must not outlive the fix for agents whose jsonl never mutates again.
- Otherwise → re-parse the jsonl, compute fields fresh, and return.

The function does NOT write any cache file — the caller (orchestrator) owns
cache persistence.

Spec: see docs/plans/20260826-status-line-model-cost-columns.md (Task 3).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import status_line
from status_line import (
    _AGENT_CACHE_FIELDS,
    _STATUS_REV,
    _compute_agents,
    compute_agent_snapshot,
)


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
# Real-shape CC 2.1.224 API-error death (session 9b7971ff): synthetic
# final assistant event with the error markers at the EVENT top level,
# stop_reason 'stop_sequence', <synthetic> model, zero usage.
AGENT_ERR_TOP_LEVEL = FIXTURES_DIR / "agent_err_top_level.jsonl"
# Agent that switches models mid-session: two kimi-k3 events (in=10+20,
# out=5+8, cached=100+200) then one glm-5.3 event (in=30, out=12,
# cached=300). Cumulative totals: in=60, out=25, cached=600.
AGENT_MODEL_SWITCH = FIXTURES_DIR / "agent_model_switch.jsonl"

META_NORMAL = FIXTURES_DIR / "meta_normal.json"
META_STOPPED_BY_USER = FIXTURES_DIR / "meta_stopped_by_user.json"


def _agent_id(jsonl_path: Path) -> str:
    """Mirror the convention: agentId is the jsonl filename without extension."""
    return jsonl_path.stem


# ---------------------------------------------------------------------------
# happy path: agent_ok
# ---------------------------------------------------------------------------

def test_agent_ok_full_snapshot() -> None:
    """agent_ok.jsonl + meta_normal.json → status="ok", CUMULATIVE breakdown
    over both assistant events (in=50+100, out=20+50, cached=150+300;
    cache_creation never surfaced), per-model dict with one record,
    description from meta, agentId from filename, toolUseId from meta,
    last_uuid = last assistant uuid.

    No `tokens` field — replaced by three breakdown fields per plan Task 1.
    """
    result = compute_agent_snapshot(AGENT_OK, META_NORMAL, cache_entry=None)

    assert result["status"] == "ok"
    # Cumulative over BOTH assistant events: input=50+100, output=20+50,
    # cache_read=150+300; cache_creation NOT in any column.
    assert result["tokens_in"] == 150
    assert result["tokens_out"] == 70
    assert result["tokens_cached"] == 450
    # Both events carry model claude-opus-4-1 → one per-model record with
    # the same cumulative numbers.
    assert result["models"] == {
        "claude-opus-4-1": {"in": 150, "out": 70, "cached": 450}
    }
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

    Cumulative over both assistant events: input=80+50, output=40+10,
    cache_read=200+100 (cache_creation=0 throughout).
    Breakdown values retained even on err status.
    """
    result = compute_agent_snapshot(
        AGENT_ERR_RATE_LIMIT, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "err"
    assert result["tokens_in"] == 130
    assert result["tokens_out"] == 50
    assert result["tokens_cached"] == 300
    assert result["description"] == "Fixer: smells findings"
    assert result["agentId"] == _agent_id(AGENT_ERR_RATE_LIMIT)
    assert result["last_uuid"] == "b0000000-0000-0000-0000-000000000004"


def test_agent_err_server_error() -> None:
    """agent_err_server_error.jsonl → status="err", breakdown from the single
    assistant event (cumulative == last-event values here): input=60,
    cache_read=120, output=15.
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
    is a user event with 'Request interrupted by user'. The assistant event
    is mid-flow (stop_reason=tool_use), with breakdown retained per plan.
    Single assistant event: input=30, cache_read=60, output=10.
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
    is non-zero (no longer blanked by run status).

    Previously `tokens=None` for run; since Task 1, breakdown fields are
    always populated. The user sees current values, not blanks.
    """
    result = compute_agent_snapshot(
        AGENT_RUNNING, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "run"
    # Single assistant event: input=40, cache_read=80, output=15.
    assert result["tokens_in"] == 40
    assert result["tokens_out"] == 15
    assert result["tokens_cached"] == 80
    # No `tokens` field anywhere.
    assert "tokens" not in result


def test_agent_no_assistant() -> None:
    """agent_no_assistant.jsonl (only user events) → status='err',
    tokens_in/out/cached all zero, models empty.

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
    assert result["models"] == {}
    assert result["last_uuid"] is None
    assert "tokens" not in result


def test_agent_no_assistant_stopped_by_user_override() -> None:
    """agent_no_assistant.jsonl + meta_stopped_by_user.json → the
    "0 assistant events" override yields "stop" (not "err") when
    meta.stoppedByUser=true. models stays empty, totals zero.
    """
    result = compute_agent_snapshot(
        FIXTURES_DIR / "agent_no_assistant.jsonl",
        META_STOPPED_BY_USER,
        cache_entry=None,
    )

    assert result["status"] == "stop"
    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert result["tokens_cached"] == 0
    assert result["models"] == {}


# ---------------------------------------------------------------------------
# cumulative + per-model accumulation (new per 20260826 Task 3)
# ---------------------------------------------------------------------------

def test_cumulative_totals_across_three_events() -> None:
    """agent_model_switch.jsonl → tokens_* are cumulative over ALL assistant
    events with usage (in=10+20+30, out=5+8+12, cached=100+200+300), NOT the
    last event's values (which would be 30/12/300). cache_creation is never
    part of cached.
    """
    result = compute_agent_snapshot(AGENT_MODEL_SWITCH, META_NORMAL, cache_entry=None)

    assert result["tokens_in"] == 60
    assert result["tokens_out"] == 25
    assert result["tokens_cached"] == 600


def test_model_switch_two_records_in_models() -> None:
    """agent_model_switch.jsonl → models holds one record per model id:
    kimi-k3 (events 1-2) and glm-5.3 (event 3). Key order follows FIRST
    appearance in the scan. Each record's sums are that model's events only
    — no cross-model mixing. last_uuid is the LAST assistant event's uuid
    regardless of model.
    """
    result = compute_agent_snapshot(AGENT_MODEL_SWITCH, META_NORMAL, cache_entry=None)

    assert result["models"] == {
        "kimi-k3": {"in": 30, "out": 13, "cached": 300},
        "glm-5.3": {"in": 30, "out": 12, "cached": 300},
    }
    # Key order = first appearance in the scan.
    assert list(result["models"].keys()) == ["kimi-k3", "glm-5.3"]
    # Per-model records sum to the cumulative totals.
    totals = {"in": 0, "out": 0, "cached": 0}
    for rec in result["models"].values():
        for key in totals:
            totals[key] += rec[key]
    assert totals == {"in": result["tokens_in"], "out": result["tokens_out"],
                      "cached": result["tokens_cached"]}
    # Last event is the glm-5.3 end_turn assistant → ok; its uuid wins.
    assert result["status"] == "ok"
    assert result["last_uuid"] == "a1000000-0000-0000-0000-000000000005"


def test_synthetic_zero_usage_event_keeps_model_record(tmp_path: Path) -> None:
    """A <synthetic>-style assistant event (zero usage) still creates its
    model record with zeros — zero rows are skipped at RENDER time, not by
    the accumulator (mirrors _scan_main_jsonl's per_model contract).
    """
    jsonl = tmp_path / "agent-synthetic.jsonl"
    jsonl.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg-synthetic",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "[Request interrupted]"}],
                "model": "<synthetic>",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
            "uuid": "a2000000-0000-0000-0000-000000000002",
        })
        + "\n"
    )

    result = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert result["models"] == {"<synthetic>": {"in": 0, "out": 0, "cached": 0}}
    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert result["tokens_cached"] == 0


def test_malformed_token_values_coerce_to_zero_not_raise(tmp_path: Path) -> None:
    """[review follow-up] A corrupt usage value (non-numeric string / None)
    must not raise out of compute_agent_snapshot — the forward scan
    converts EVERY event's usage, so one bad mid-file value used to raise
    ValueError (int('abc')) and degrade the whole status line to the
    fallback header via main()'s catch-all, violating the documented
    "compute_agent_snapshot never raises" invariant. _to_int coerces to 0.
    """
    jsonl = tmp_path / "agent-bad-tokens.jsonl"
    jsonl.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hi"}],
                "model": "glm-5.3",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": "abc",
                    "output_tokens": 3,
                    "cache_read_input_tokens": None,
                },
            },
            "uuid": "a3000000-0000-0000-0000-000000000001",
        })
        + "\n"
    )

    result = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 3
    assert result["tokens_cached"] == 0
    assert result["models"] == {"glm-5.3": {"in": 0, "out": 3, "cached": 0}}


def test_non_str_uuid_yields_none_not_raw_value(tmp_path: Path) -> None:
    """[review follow-up] A corrupt uuid (non-str) on the last assistant
    event must yield last_uuid=None, not the raw value — last_uuid flows
    into the snapshot, the agents cache and the cache-key equality
    check, and the main scan already applies the same isinstance guard.
    """
    jsonl = tmp_path / "agent-bad-uuid.jsonl"
    jsonl.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hi"}],
                "model": "glm-5.3",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                },
            },
            "uuid": 12345,
        })
        + "\n"
    )

    result = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert result["tokens_in"] == 7
    assert result["last_uuid"] is None


def test_assistant_event_without_model_field_uses_empty_key(tmp_path: Path) -> None:
    """An assistant event WITH usage but NO model field accumulates under
    the "" key in `models` (mirrors _scan_main_jsonl) — the render layer
    shows the row with empty model/cost cells.
    """
    jsonl = tmp_path / "agent-no-model.jsonl"
    jsonl.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 5,
                },
            },
            "uuid": "a4000000-0000-0000-0000-000000000001",
        })
        + "\n"
    )

    result = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert result["tokens_in"] == 100
    assert result["tokens_out"] == 10
    assert result["tokens_cached"] == 5
    assert result["models"] == {"": {"in": 100, "out": 10, "cached": 5}}


# ---------------------------------------------------------------------------
# breakdown-field edge cases (per Task 1)
# ---------------------------------------------------------------------------

def test_breakdown_absent_usage_block_yields_zeros(tmp_path: Path) -> None:
    """Assistant event present but `message.usage` absent → all three
    breakdown fields = 0 and NO model record (per-model accumulation is
    gated on a usage block, mirroring _scan_main_jsonl). Synthesize a
    degenerate assistant event with no usage key.
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
    assert result["models"] == {}


def test_breakdown_zero_when_no_assistant_events(tmp_path: Path) -> None:
    """Empty jsonl (no events at all) → last_event is None → all three
    fields = 0, models empty. Distinct from `agent_no_assistant.jsonl`
    (which has user events) — this covers the truly-empty case.
    """
    jsonl = tmp_path / "agent-empty.jsonl"
    jsonl.write_text("")  # zero lines

    result = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert result["tokens_cached"] == 0
    assert result["models"] == {}
    assert result["last_uuid"] is None


def test_breakdown_missing_jsonl_yields_zeros(tmp_path: Path) -> None:
    """Missing jsonl file → no events → all three fields = 0, models empty."""
    missing = tmp_path / "agent-does-not-exist.jsonl"
    assert not missing.exists()

    result = compute_agent_snapshot(missing, META_NORMAL, cache_entry=None)

    assert result["tokens_in"] == 0
    assert result["tokens_out"] == 0
    assert result["tokens_cached"] == 0
    assert result["models"] == {}
    assert result["last_uuid"] is None



# ---------------------------------------------------------------------------
# meta-driven overrides
# ---------------------------------------------------------------------------

def test_meta_stopped_by_user_triggers_stop() -> None:
    """agent_ok.jsonl + meta_stopped_by_user.json → status='stop'.

    Even though the agent finished cleanly (last assistant end_turn),
    meta.stoppedByUser=true overrides to 'stop'. Cumulative breakdown still
    computed.
    """
    result = compute_agent_snapshot(
        AGENT_OK, META_STOPPED_BY_USER, cache_entry=None
    )

    assert result["status"] == "stop"
    assert result["tokens_in"] == 150
    assert result["tokens_out"] == 70
    assert result["tokens_cached"] == 450


def test_meta_long_description_truncated() -> None:
    """agent_ok.jsonl + meta_long_description.json (60 chars) → description
    is 40 chars long, ends with U+2026 '…', first 39 chars match original.
    """
    meta_path = FIXTURES_DIR / "meta_long_description.json"
    original = json.loads(meta_path.read_text(encoding="utf-8"))["description"]
    assert len(original) == 60

    result = compute_agent_snapshot(AGENT_OK, meta_path, cache_entry=None)

    desc = result["description"]
    assert len(desc) == 40
    assert desc.endswith("…"), f"description should end with …, got {desc!r}"
    # First 39 chars match the original first 39 chars.
    assert desc[:39] == original[:39]


def test_meta_missing_fallback(tmp_path: Path) -> None:
    """Missing meta or empty description → fallback to agentType, then 'unknown'."""
    # Case 1: meta file with empty description but valid agentType.
    meta_with_type = tmp_path / "_meta_empty_desc.json"
    meta_with_type.write_text(json.dumps({
        "agentType": "Explore",
        "toolUseId": "toolu_999",
    }))
    result = compute_agent_snapshot(
        AGENT_OK, meta_with_type, cache_entry=None
    )
    assert result["description"] == "Explore"
    assert result["toolUseId"] == "toolu_999"
    # Status still computed from last event.
    assert result["status"] == "ok"

    # Case 2: completely missing meta path → fallback to 'unknown'.
    missing_meta = tmp_path / "_meta_definitely_missing.json"
    assert not missing_meta.exists()

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
    mtime_meta AND all three breakdown fields AND `models` AND the four
    time-segmentation fields → function returns the cached dict without
    re-parsing. Sentinel values on the breakdown and time fields prove
    the cache was used (and that the time fields pass through the hit
    path unchanged — the orchestrator reads them on hit cycles too).
    """
    mtime = AGENT_OK.stat().st_mtime
    last_uuid = "a0000000-0000-0000-0000-000000000004"
    models_sentinel = {"cached-model": {"in": 1, "out": 2, "cached": 3}}
    qa_pauses_sentinel = [[100.5, 150.25]]

    cache_entry = {
        "agentId": _agent_id(AGENT_OK),
        "status": "ok",
        "status_rev": _STATUS_REV,
        "tokens_in": 11_111,
        "tokens_out": 22_222,
        "tokens_cached": 33_333,
        "models": models_sentinel,
        "description": "from-cache",
        "toolUseId": "toolu_cached",
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime,
        "mtime_meta": META_NORMAL.stat().st_mtime,
        "ts_first": 1000.0,
        "ts_last": 1400.5,
        "qa_pauses": qa_pauses_sentinel,
        "qa_open_ts": 0.0,
    }

    result = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_entry
    )

    # If cache was used, the sentinels survive.
    assert result["tokens_in"] == 11_111
    assert result["tokens_out"] == 22_222
    assert result["tokens_cached"] == 33_333
    assert result["models"] == models_sentinel
    assert result["description"] == "from-cache"
    assert result["toolUseId"] == "toolu_cached"
    assert result["ts_first"] == 1000.0
    assert result["ts_last"] == 1400.5
    assert result["qa_pauses"] == qa_pauses_sentinel
    assert result["qa_open_ts"] == 0.0


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
        "models": {"m": {"in": 1, "out": 1, "cached": 1}},
        "description": "stale-no-in",
        "toolUseId": "toolu_stale",
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime,
        "mtime_meta": mtime_meta,
    }
    r_a = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_no_in
    )
    assert r_a["tokens_in"] == 150, (
        "cache miss expected: missing tokens_in triggers re-parse"
    )
    assert r_a["tokens_out"] == 70
    assert r_a["tokens_cached"] == 450
    assert r_a["description"] == "Fixer: smells findings"

    # Case B: missing tokens_out entirely.
    cache_no_out = dict(cache_no_in)
    cache_no_out["tokens_in"] = 111
    del cache_no_out["tokens_out"]
    r_b = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_no_out
    )
    assert r_b["tokens_out"] == 70, (
        "cache miss expected: missing tokens_out triggers re-parse"
    )
    assert r_b["tokens_in"] == 150
    assert r_b["tokens_cached"] == 450

    # Case C: missing tokens_cached entirely.
    cache_no_cached = dict(cache_no_in)
    cache_no_cached["tokens_in"] = 111
    cache_no_cached["tokens_out"] = 222
    del cache_no_cached["tokens_cached"]
    r_c = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_no_cached
    )
    assert r_c["tokens_cached"] == 450, (
        "cache miss expected: missing tokens_cached triggers re-parse"
    )
    assert r_c["tokens_in"] == 150
    assert r_c["tokens_out"] == 70


def test_cache_miss_when_models_missing() -> None:
    """[upgrade path] Cache entry matches the full key (last_uuid +
    mtime_jsonl + mtime_meta) and carries all breakdown fields, but lacks
    `models` (pre-model-columns schema) → cache MISS, forward re-parse
    rebuilds the per-model dict. Without this check an upgraded hook would
    render empty model/cost cells for agents until their jsonl next
    mutated. Same field-presence guard pattern as the breakdown fields and
    the main cache's per_model.
    """
    mtime = AGENT_OK.stat().st_mtime
    mtime_meta = META_NORMAL.stat().st_mtime

    cache_no_models = {
        "agentId": "wrong-id",
        "status": "run",
        "tokens_in": 111,
        "tokens_out": 222,
        "tokens_cached": 333,
        "description": "stale-no-models",
        "toolUseId": "toolu_stale",
        "last_uuid": "a0000000-0000-0000-0000-000000000004",
        "mtime_jsonl": mtime,
        "mtime_meta": mtime_meta,
    }

    result = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_no_models
    )

    # Fresh values prove the re-parse ran (old code: sentinels survive).
    assert result["tokens_in"] == 150
    assert result["tokens_out"] == 70
    assert result["tokens_cached"] == 450
    assert result["models"] == {
        "claude-opus-4-1": {"in": 150, "out": 70, "cached": 450}
    }
    assert result["description"] == "Fixer: smells findings"


def test_cache_miss_when_status_rev_missing_or_old() -> None:
    """[upgrade path] Cache entry matches the full key (last_uuid +
    mtime_jsonl + mtime_meta) and carries every breakdown/models/time
    field, but its status_rev is absent (pre-rev cache) or an OLD rev →
    cache MISS, forward re-parse recomputes `status` under the current
    logic. This is what heals already-dead agents after a
    classification fix: their jsonl never mutates again, so the stale
    cached "run" would otherwise survive the fix forever (observed in
    session 9b7971ff: five 429-dead agents rendering as [run])."""
    mtime = AGENT_OK.stat().st_mtime
    mtime_meta = META_NORMAL.stat().st_mtime

    def _entry(rev):
        return {
            "status": "run",  # the misclassification being healed
            "tokens_in": 9_999_999,
            "tokens_out": 9_999_999,
            "tokens_cached": 9_999_999,
            "models": {"stale-model": {"in": 1, "out": 1, "cached": 1}},
            "description": "stale-pre-rev",
            "toolUseId": "toolu_stale",
            "last_uuid": "a0000000-0000-0000-0000-000000000004",
            "mtime_jsonl": mtime,
            "mtime_meta": mtime_meta,
            "ts_first": 1000.0,
            "ts_last": 1400.5,
            "qa_pauses": [],
            "qa_open_ts": 0.0,
            **({"status_rev": rev} if rev is not None else {}),
        }

    # Case A: pre-rev entry (no status_rev key at all).
    result_a = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=_entry(None)
    )
    assert result_a["status"] == "ok", (
        "missing status_rev must be a miss: status recomputed from jsonl"
    )
    assert result_a["tokens_in"] == 150

    # Case B: old rev (any value != current _STATUS_REV).
    result_b = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=_entry(_STATUS_REV - 1)
    )
    assert result_b["status"] == "ok"
    assert result_b["tokens_in"] == 150


def test_snapshot_carries_current_status_rev() -> None:
    """Every fresh (cache-miss) snapshot stamps the status-logic revision
    it was classified under, so the NEXT cache-hit check can compare it."""
    result = compute_agent_snapshot(AGENT_OK, META_NORMAL, cache_entry=None)
    assert result["status_rev"] == _STATUS_REV


def test_err_top_level_fixture_snapshot() -> None:
    """Full snapshot over the real-shape 429-death fixture: status 'err'
    (the fix under test), breakdown ONLY from the real assistant event
    (in=200, out=50, cached=400 — the synthetic event's zero usage adds
    nothing), models keeps the zero-token <synthetic> key (the render
    layer skips zero rows), and the synthetic event's uuid is the
    last_uuid (it IS an assistant event — the cache keys on it)."""
    result = compute_agent_snapshot(
        AGENT_ERR_TOP_LEVEL, META_NORMAL, cache_entry=None
    )

    assert result["status"] == "err"
    assert result["tokens_in"] == 200
    assert result["tokens_out"] == 50
    assert result["tokens_cached"] == 400
    assert result["models"] == {
        "glm-5.3": {"in": 200, "out": 50, "cached": 400},
        "<synthetic>": {"in": 0, "out": 0, "cached": 0},
    }
    assert result["last_uuid"] == "e0000000-0000-0000-0000-000000000004"


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
        "models": {"stale-model": {"in": 1, "out": 1, "cached": 1}},
        "description": "stale-description",
        "toolUseId": "toolu_stale",
        "last_uuid": "stale-uuid-that-doesnt-match",
        "mtime_jsonl": mtime,
        "mtime_meta": META_NORMAL.stat().st_mtime,
    }
    result = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_wrong_uuid
    )
    assert result["tokens_in"] == 150
    assert result["tokens_out"] == 70
    assert result["tokens_cached"] == 450
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
    assert result2["tokens_in"] == 150
    assert result2["tokens_out"] == 70
    assert result2["tokens_cached"] == 450
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

    # Cache built when meta was at v1. Includes the three breakdown fields,
    # `models`, the four time fields and the current status_rev so the only
    # thing invalidating the cache is mtime_meta.
    cache_v1 = {
        "agentId": "agent-test",
        "status": "ok",
        "status_rev": _STATUS_REV,
        "tokens_in": 9_999_999,  # sentinel — should not survive
        "tokens_out": 9_999_999,
        "tokens_cached": 9_999_999,
        "models": {"v1-model": {"in": 1, "out": 1, "cached": 1}},
        "description": "stale-from-v1",
        "toolUseId": "toolu_v1",
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime_jsonl,
        "mtime_meta": mtime_meta_v1,
        "ts_first": 1000.0,
        "ts_last": 1400.5,
        "qa_pauses": [[1100.0, 1150.0]],
        "qa_open_ts": 0.0,
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
# time-segmentation fields (per 20260827-status-line-time-columns Task 4)
#
# _scan_agent_jsonl additionally collects ts_first/ts_last (epoch bounds of
# ALL stamped events, any type) and AskUserQuestion pause bookkeeping:
# qa_pauses holds closed [question → next user event] pairs; qa_open_ts is
# the question ts of a still-unanswered pause (0.0 when none). The fields
# flow through compute_agent_snapshot into the agents cache so the
# orchestrator can extend/handle live work windows on cache-hit cycles too.
# ---------------------------------------------------------------------------

# Base instant all synthetic stamps are derived from — expected epochs are
# computed from the same tz-aware datetime, so the assertions stay exact
# regardless of the machine's local timezone.
_TIME_BASE = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
_T0 = _TIME_BASE.timestamp()


def _stamp(offset_s: float) -> str:
    """ISO 8601 UTC stamp (ms precision) offset seconds from _TIME_BASE."""
    dt = _TIME_BASE + timedelta(seconds=offset_s)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _t_user(ts: str | None, *, interrupt: bool = False) -> dict:
    """A string-content user event (prompt or interrupt); ts=None omits it."""
    text = "[Request interrupted by user]" if interrupt else "do the thing"
    event: dict = {"type": "user", "message": {"content": text}}
    if ts is not None:
        event["timestamp"] = ts
    return event


def _t_tool_result_user(ts: str | None) -> dict:
    """A list-content user event (a tool_result — activity, not boundary)."""
    event: dict = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "done"}]},
    }
    if ts is not None:
        event["timestamp"] = ts
    return event


def _t_assistant(
    ts: str | None,
    *,
    stop_reason: str = "end_turn",
    uuid: str = "uuid-x",
    qa_question: bool = False,
) -> dict:
    """An assistant event carrying usage; qa_question=True swaps the content
    for an AskUserQuestion tool_use block. ts=None omits the timestamp."""
    content: list[dict] = [{"type": "text", "text": "working"}]
    if qa_question:
        content = [
            {
                "type": "tool_use",
                "id": "toolu_qa",
                "name": "AskUserQuestion",
                "input": {},
            }
        ]
    event: dict = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "glm-5.3",
            "stop_reason": stop_reason,
            "content": content,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_input_tokens": 0,
            },
        },
        "uuid": uuid,
    }
    if ts is not None:
        event["timestamp"] = ts
    return event


def _write_jsonl(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def test_ts_first_ts_last_span_all_stamped_events(tmp_path: Path) -> None:
    """ts_first/ts_last anchor on ANY typed event carrying a parseable
    timestamp — user prompts and tool_result events included, not only
    assistant events (mirrors the main scan's time_first_ts rule)."""
    jsonl = _write_jsonl(tmp_path / "agent-times.jsonl", [
        _t_assistant(_stamp(0), uuid="a1"),
        _t_tool_result_user(_stamp(20)),
        _t_assistant(_stamp(40), stop_reason="tool_use", uuid="a2"),
        _t_user(_stamp(60)),
    ])

    snap = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert snap["ts_first"] == pytest.approx(_T0)
    assert snap["ts_last"] == pytest.approx(_T0 + 60)


def test_unstamped_events_skipped_for_time_bounds(tmp_path: Path) -> None:
    """Events without a (parseable) timestamp are silently skipped for
    time purposes; stamped neighbors define both bounds. Mixed stream:
    unstamped user/assistant lines between stamped ones must not shift
    or zero ts_first/ts_last."""
    jsonl = _write_jsonl(tmp_path / "agent-mixed.jsonl", [
        _t_user(None),
        _t_assistant(_stamp(5), uuid="a1"),
        _t_assistant(None, stop_reason="tool_use", uuid="a2"),
        _t_tool_result_user(_stamp(25)),
    ])

    snap = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert snap["ts_first"] == pytest.approx(_T0 + 5)
    assert snap["ts_last"] == pytest.approx(_T0 + 25)


def test_all_unstamped_events_yield_zero_time_fields(tmp_path: Path) -> None:
    """No parseable timestamp anywhere → both bounds stay 0.0 and no QA
    bookkeeping exists (zeros mean "degrade to empty time cells" for the
    renderer, NOT "zero duration")."""
    jsonl = _write_jsonl(tmp_path / "agent-nostamps.jsonl", [
        _t_user(None),
        _t_assistant(None, uuid="a1"),
    ])

    snap = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert snap["ts_first"] == 0.0
    assert snap["ts_last"] == 0.0
    assert snap["qa_pauses"] == []
    assert snap["qa_open_ts"] == 0.0


def test_qa_pause_closed_by_user_reply(tmp_path: Path) -> None:
    """AskUserQuestion tool_use opens a pause at its ts; the NEXT user event
    of any kind closes it → exactly one closed [question, answer] pair in
    qa_pauses and qa_open_ts back to 0.0. Here the closer is a list-content
    tool_result user event."""
    jsonl = _write_jsonl(tmp_path / "agent-qa-closed.jsonl", [
        _t_assistant(_stamp(10), stop_reason="end_turn", uuid="a1"),
        _t_assistant(
            _stamp(60), qa_question=True, stop_reason="tool_use", uuid="a2"
        ),
        _t_tool_result_user(_stamp(90)),
        _t_assistant(_stamp(120), stop_reason="end_turn", uuid="a3"),
    ])

    snap = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert len(snap["qa_pauses"]) == 1
    start, end = snap["qa_pauses"][0]
    assert start == pytest.approx(_T0 + 60)
    assert end == pytest.approx(_T0 + 90)
    assert snap["qa_open_ts"] == 0.0


def test_qa_pause_closed_by_interrupt_string_user(tmp_path: Path) -> None:
    """The user reply that resolves a QA pause may also be a string-content
    user event (e.g. "[Request interrupted by user]") — same closing rule,
    mirroring the main scan's 'any user event resolves a hanging QA'."""
    jsonl = _write_jsonl(tmp_path / "agent-qa-interrupt.jsonl", [
        _t_assistant(_stamp(30), qa_question=True, stop_reason="tool_use", uuid="a1"),
        _t_user(_stamp(50), interrupt=True),
    ])

    snap = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert len(snap["qa_pauses"]) == 1
    start, end = snap["qa_pauses"][0]
    assert start == pytest.approx(_T0 + 30)
    assert end == pytest.approx(_T0 + 50)
    assert snap["qa_open_ts"] == 0.0


def test_multiple_qa_pauses_accumulate_in_order(tmp_path: Path) -> None:
    """Two full question→answer cycles → two closed pairs, in file order;
    timestamps inside each pair preserve the actual wait spans."""
    jsonl = _write_jsonl(tmp_path / "agent-qa-twice.jsonl", [
        _t_assistant(_stamp(0), stop_reason="end_turn", uuid="a1"),
        _t_assistant(_stamp(60), qa_question=True, stop_reason="tool_use", uuid="a2"),
        _t_tool_result_user(_stamp(90)),
        _t_tool_result_user(_stamp(110)),
        _t_assistant(_stamp(200), qa_question=True, stop_reason="tool_use", uuid="a3"),
        _t_tool_result_user(_stamp(230)),
    ])

    snap = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    pauses = snap["qa_pauses"]
    assert len(pauses) == 2
    assert pauses[0][0] == pytest.approx(_T0 + 60)
    assert pauses[0][1] == pytest.approx(_T0 + 90)
    assert pauses[1][0] == pytest.approx(_T0 + 200)
    assert pauses[1][1] == pytest.approx(_T0 + 230)
    assert snap["qa_open_ts"] == 0.0


def test_unanswered_qa_sets_open_ts(tmp_path: Path) -> None:
    """An AskUserQuestion with NO following user event (the last assistant
    event of the jsonl) leaves no closed pair but records qa_open_ts — the
    orchestrator extends this gap as the agent's wait time."""
    jsonl = _write_jsonl(tmp_path / "agent-qa-open.jsonl", [
        _t_assistant(_stamp(0), stop_reason="end_turn", uuid="a1"),
        _t_assistant(_stamp(70), qa_question=True, stop_reason="tool_use", uuid="a2"),
    ])

    snap = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert snap["qa_pauses"] == []
    assert snap["qa_open_ts"] == pytest.approx(_T0 + 70)


def test_second_qa_while_paused_not_double_tracked(tmp_path: Path) -> None:
    """A second AskUserQuestion while a pause is already open must not reset
    the open ts — one pause stays one pause (same guard as the main scan),
    closed once by the next user event."""
    jsonl = _write_jsonl(tmp_path / "agent-qa-nested.jsonl", [
        _t_assistant(_stamp(0), stop_reason="end_turn", uuid="a1"),
        _t_assistant(_stamp(60), qa_question=True, stop_reason="tool_use", uuid="a2"),
        _t_assistant(_stamp(80), qa_question=True, stop_reason="tool_use", uuid="a3"),
        _t_tool_result_user(_stamp(100)),
    ])

    snap = compute_agent_snapshot(jsonl, META_NORMAL, cache_entry=None)

    assert len(snap["qa_pauses"]) == 1
    start, end = snap["qa_pauses"][0]
    assert start == pytest.approx(_T0 + 60)   # NOT reset by the second QA at +80
    assert end == pytest.approx(_T0 + 100)
    assert snap["qa_open_ts"] == 0.0


def test_time_fields_zero_for_empty_broken_or_missing_jsonl(
    tmp_path: Path,
) -> None:
    """Empty file, file of unparseable lines, and a missing file (OSError
    degradation path) all yield the zeroed time quartet — never garbage,
    never an exception ("the hook cannot crash")."""
    empty = tmp_path / "agent-empty-time.jsonl"
    empty.write_text("", encoding="utf-8")  # zero lines
    garbage = tmp_path / "agent-garbage-time.jsonl"
    garbage.write_text("not json at all\n{broken\n", encoding="utf-8")
    missing = tmp_path / "agent-absent-time.jsonl"

    for path in (empty, garbage, missing):
        snap = compute_agent_snapshot(path, META_NORMAL, cache_entry=None)
        assert snap["ts_first"] == 0.0, f"path={path!r}"
        assert snap["ts_last"] == 0.0, f"path={path!r}"
        assert snap["qa_pauses"] == [], f"path={path!r}"
        assert snap["qa_open_ts"] == 0.0, f"path={path!r}"


def test_agent_cache_fields_include_time_keys() -> None:
    """_AGENT_CACHE_FIELDS must carry the four time fields so they persist
    across invocations — the orchestrator needs them on cache-HIT cycles
    (live-run extension happens per render, not per re-parse)."""
    required = {"ts_first", "ts_last", "qa_pauses", "qa_open_ts"}
    assert required.issubset(set(_AGENT_CACHE_FIELDS)), (
        f"_AGENT_CACHE_FIELDS missing time keys: "
        f"{required - set(_AGENT_CACHE_FIELDS)}"
    )


def test_cache_miss_when_any_time_field_missing() -> None:
    """[upgrade path] A pre-time-column cache matches the full key AND the
    breakdown/models guards but lacks one of the four time fields → cache
    MISS, forward re-parse rebuilds everything. Without this check each
    agent would show degraded/stale time columns until its jsonl next
    mutated. Same field-presence pattern as the breakdown/models guards.
    """
    base = {
        "agentId": "wrong-id",
        "status": "run",
        "tokens_in": 111,
        "tokens_out": 222,
        "tokens_cached": 333,
        "models": {"m": {"in": 1, "out": 1, "cached": 1}},
        "description": "stale-no-time",
        "toolUseId": "toolu_stale",
        "last_uuid": "a0000000-0000-0000-0000-000000000004",
        "mtime_jsonl": AGENT_OK.stat().st_mtime,
        "mtime_meta": META_NORMAL.stat().st_mtime,
        # sentinels: fresh rebuild values must replace every one of them
        "ts_first": -1.0,
        "ts_last": -2.0,
        "qa_pauses": [[-7.0, -6.0]],
        "qa_open_ts": -3.0,
    }

    missing_cases = ("ts_first", "ts_last", "qa_pauses", "qa_open_ts")
    for absent in missing_cases:
        entry = dict(base)
        del entry[absent]
        result = compute_agent_snapshot(
            AGENT_OK, META_NORMAL, cache_entry=entry
        )
        # Fresh token totals prove the re-parse ran for EVERY absent field.
        assert result["tokens_in"] == 150, (
            f"cache miss expected when {absent!r} is missing"
        )
        # Rebuilt snapshot carries plausible rebuilt values, not sentinels.
        assert result["ts_first"] != -1.0
        assert result["ts_last"] != -2.0
        assert result["qa_pauses"] != [[-7.0, -6.0]]
        assert result["qa_open_ts"] != -3.0
        assert isinstance(result["ts_first"], float)
        assert isinstance(result["ts_last"], float)
        assert isinstance(result["qa_pauses"], list)
        assert isinstance(result["qa_open_ts"], float)


# ---------------------------------------------------------------------------
# _compute_agents orchestrator override


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
    The fixture-pair copy itself lives in `_add_agent_to_session` below.
    """
    session_dir = tmp_path / "session-abc"
    agent_id = "agent-test"
    _add_agent_to_session(session_dir, agent_id, src_jsonl, src_meta)
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


# ---------------------------------------------------------------------------
# _compute_agents multi-dir merge + agentId dedup
# (added per 20260826-merge-subagents-across-session-dirs: one session id can
# live in several project dirs — main checkout + worktree — and agents are
# spread across their subagents/ trees)
# ---------------------------------------------------------------------------

def _add_agent_to_session(
    session_dir: Path, agent_id: str, src_jsonl: Path, src_meta: Path
) -> Path:
    """Write `agent-<id>.{jsonl,meta.json}` into session_dir/subagents/ by
    copying the given fixture pair. Creates the dir tree on demand. Returns
    the jsonl path (callers rarely need it — mostly for parse-count asserts).
    """
    subagents_dir = session_dir / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    dst_jsonl = subagents_dir / f"{agent_id}.jsonl"
    dst_meta = subagents_dir / f"{agent_id}.meta.json"
    dst_jsonl.write_bytes(src_jsonl.read_bytes())
    dst_meta.write_bytes(src_meta.read_bytes())
    return dst_jsonl


def test_compute_agents_merges_agents_across_session_dirs(
    tmp_path: Path,
) -> None:
    """Agents with DIFFERENT agentIds spread over two session dirs → the
    result is the union of both (each CC project dir owns part of the
    session's subagents; neither dir alone has the full picture)."""
    dir_main = tmp_path / "project-main" / "session-abc"
    dir_worktree = tmp_path / "project-worktree" / "session-abc"
    _add_agent_to_session(dir_main, "agent-one", AGENT_RUNNING, META_NORMAL)
    _add_agent_to_session(dir_worktree, "agent-two", AGENT_OK, META_NORMAL)
    agents_cache = tmp_path / "agents_cache.json"

    agents = _compute_agents([dir_main, dir_worktree], agents_cache)

    assert {a["agentId"] for a in agents} == {"agent-one", "agent-two"}
    statuses = {a["agentId"]: a["status"] for a in agents}
    assert statuses["agent-one"] == "run"
    assert statuses["agent-two"] == "ok"


def test_compute_agents_dedups_same_agent_id_first_dir_wins(
    tmp_path: Path, monkeypatch
) -> None:
    """The SAME agentId in two session dirs → exactly one snapshot, built
    from the FIRST directory of the list (first-dir wins), and the duplicate
    is skipped at the path level — compute_agent_snapshot never parses it.
    Distinguished by content: dir A holds agent_running (status "run"),
    dir B holds agent_ok (status "ok") under the same filename."""
    dir_a = tmp_path / "project-a" / "session-abc"
    dir_b = tmp_path / "project-b" / "session-abc"
    jsonl_a = _add_agent_to_session(dir_a, "agent-test", AGENT_RUNNING, META_NORMAL)
    jsonl_b = _add_agent_to_session(dir_b, "agent-test", AGENT_OK, META_NORMAL)
    agents_cache = tmp_path / "agents_cache.json"

    real_snapshot = status_line.compute_agent_snapshot
    parsed: list[Path] = []

    def counting_snapshot(jsonl_path, meta_path, cache_entry):
        parsed.append(jsonl_path)
        return real_snapshot(jsonl_path, meta_path, cache_entry)

    monkeypatch.setattr(
        status_line, "compute_agent_snapshot", counting_snapshot
    )
    agents = _compute_agents([dir_a, dir_b], agents_cache)

    assert len(agents) == 1
    assert agents[0]["agentId"] == "agent-test"
    assert agents[0]["status"] == "run", (
        f"first dir must win; got {agents[0]['status']!r} "
        f"(looks like dir B's copy was used)"
    )
    assert parsed == [jsonl_a], (
        f"duplicate must be skipped before parsing; parsed={parsed!r}, "
        f"dir B copy is {jsonl_b!r}"
    )


def test_compute_agents_single_path_back_compat(tmp_path: Path) -> None:
    """A bare Path (the pre-multi-dir call shape) keeps working and gives
    the same result as the equivalent one-element list."""
    session_dir, agents_cache, _ = _make_session_with_agent(
        tmp_path, AGENT_RUNNING, META_NORMAL
    )

    from_path = _compute_agents(session_dir, agents_cache)
    from_list = _compute_agents([session_dir], agents_cache)

    assert len(from_path) == 1
    assert from_path[0]["agentId"] == "agent-test"
    assert from_path[0]["status"] == "run"
    # Same call, list-wrapped → identical outcome (agentId + status).
    assert [(a["agentId"], a["status"]) for a in from_list] == [
        (a["agentId"], a["status"]) for a in from_path
    ]


def test_compute_agents_single_str_path_is_normalized(tmp_path: Path) -> None:
    """A bare str path (easy call-site mistake: `str(path)`) is normalized
    to a one-element list instead of being iterated character by character
    — which would silently yield [] (every 1-char "dir" fails the
    subagents/ existence check) and make the agents vanish."""
    session_dir, agents_cache, _ = _make_session_with_agent(
        tmp_path, AGENT_RUNNING, META_NORMAL
    )

    agents = _compute_agents(str(session_dir), agents_cache)

    assert len(agents) == 1
    assert agents[0]["agentId"] == "agent-test"
    assert agents[0]["status"] == "run"


def test_compute_agents_empty_dirs_list_returns_empty(tmp_path: Path) -> None:
    """An empty list of session dirs → no agents (no crash, no fs access)."""
    agents_cache = tmp_path / "agents_cache.json"

    assert _compute_agents([], agents_cache) == []


def test_compute_agents_skips_dir_without_subagents(
    tmp_path: Path,
) -> None:
    """A directory lacking subagents/ (e.g. an empty worktree copy) is
    skipped; the rest of the list is still processed."""
    dir_empty = tmp_path / "project-worktree" / "session-abc"
    dir_empty.mkdir(parents=True)  # exists, but no subagents/ inside
    dir_full = tmp_path / "project-main" / "session-abc"
    _add_agent_to_session(dir_full, "agent-test", AGENT_RUNNING, META_NORMAL)
    agents_cache = tmp_path / "agents_cache.json"

    agents = _compute_agents([dir_empty, dir_full], agents_cache)

    assert [a["agentId"] for a in agents] == ["agent-test"]
    assert agents[0]["status"] == "run"


def test_compute_agents_queue_override_reaches_second_dir_agent(
    tmp_path: Path,
) -> None:
    """The orchestrator queue override applies AFTER the merge, to agents
    from EVERY directory — an agent living only in the second dir must be
    overridden just like a first-dir agent (a refactor moving the override
    into the per-dir loop would otherwise drop or mis-apply it)."""
    dir_a = tmp_path / "project-main" / "session-abc"
    dir_b = tmp_path / "project-worktree" / "session-abc"
    _add_agent_to_session(dir_a, "agent-aaa", AGENT_RUNNING, META_NORMAL)
    _add_agent_to_session(dir_b, "agent-bbb", AGENT_RUNNING, META_NORMAL)
    agents_cache = tmp_path / "agents_cache.json"

    agents = _compute_agents(
        [dir_a, dir_b], agents_cache, task_notifications={"bbb": "kill"}
    )

    statuses = {a["agentId"]: a["status"] for a in agents}
    assert statuses["agent-bbb"] == "kill", (
        f"override must reach second-dir agents; got {statuses!r}"
    )
    # the notification targets only bbb — aaa keeps its snapshot status
    assert statuses["agent-aaa"] == "run"


# ---------------------------------------------------------------------------
# cache-fields invariant — _AGENT_CACHE_FIELDS is the persisted shape,
# agentId is NOT inside each entry (it's the dict key)
# ---------------------------------------------------------------------------

def test_agent_cache_fields_does_not_include_agentid() -> None:
    """[invariant] _AGENT_CACHE_FIELDS must NOT contain "agentId" — the
    per-agent cache stores agentId as the OUTER dict key, not as a field
    inside each entry. compute_agent_snapshot re-injects agentId into the
    returned dict on the cache-hit path so downstream callers see a
    uniform shape. If a future change accidentally adds "agentId" to
    _AGENT_CACHE_FIELDS, _write_agents_cache would write a redundant
    field and the agentId-reinjection path would no longer be exercised.
    """
    assert "agentId" not in _AGENT_CACHE_FIELDS, (
        f"agentId must be the cache dict key, not an inner field. "
        f"_AGENT_CACHE_FIELDS={_AGENT_CACHE_FIELDS!r}"
    )


def test_agent_cache_fields_includes_all_rendered_keys() -> None:
    """The persisted cache must carry every field the renderer needs to
    rebuild an agent line: status, tokens_in/out/cached, models,
    description, toolUseId, plus the cache-key fields (last_uuid,
    mtime_jsonl, mtime_meta)."""
    required = {
        "status",
        "tokens_in",
        "tokens_out",
        "tokens_cached",
        "models",
        "description",
        "toolUseId",
        "last_uuid",
        "mtime_jsonl",
        "mtime_meta",
    }
    assert required.issubset(set(_AGENT_CACHE_FIELDS)), (
        f"_AGENT_CACHE_FIELDS missing required keys: "
        f"{required - set(_AGENT_CACHE_FIELDS)}"
    )


def test_cache_hit_injects_agentid_into_returned_dict() -> None:
    """compute_agent_snapshot must re-inject agentId on the cache-hit
    path. Without this, _write_agents_cache raises KeyError(a['agentId'])
    and main()'s except clause silently degrades to the fallback header.
    The regression tested by test_second_call_after_cache in
    test_main_integration.py."""
    mtime = AGENT_OK.stat().st_mtime
    last_uuid = "a0000000-0000-0000-0000-000000000004"

    # Pre-upgrade-style cache entry that already has agentId inside; the
    # function should still overwrite it with the canonical agent_id
    # derived from the jsonl filename stem.
    cache_entry = {
        "agentId": "wrong-id-from-cache",
        "status": "ok",
        "status_rev": _STATUS_REV,
        "tokens_in": 11_111,
        "tokens_out": 22_222,
        "tokens_cached": 33_333,
        "models": {"cached-model": {"in": 1, "out": 2, "cached": 3}},
        "description": "from-cache",
        "toolUseId": "toolu_cached",
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime,
        "mtime_meta": META_NORMAL.stat().st_mtime,
        "ts_first": 1000.0,
        "ts_last": 1400.5,
        "qa_pauses": [],
        "qa_open_ts": 0.0,
    }

    result = compute_agent_snapshot(
        AGENT_OK, META_NORMAL, cache_entry=cache_entry
    )
    assert result["agentId"] == _agent_id(AGENT_OK), (
        f"agentId should be re-injected from jsonl filename stem, "
        f"got: {result['agentId']!r}"
    )
