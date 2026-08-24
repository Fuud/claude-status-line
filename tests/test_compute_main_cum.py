"""Tests for compute_main_cum.

compute_main_cum(jsonl_path, cache_path) reads a main session jsonl and returns
cumulative token counters (input/output/cache_creation/cache_read) plus a map of
tool_use ids to their event indices in the jsonl. Results are cached in
`cache_path` keyed by the last assistant event's uuid — if the jsonl tail hasn't
changed, the cached values are returned without re-scanning.

Cache semantics:
- If `cache_path` exists, load and compare `last_uuid` to the jsonl's last
  assistant uuid. If equal → return cached values.
- If the cache file is malformed (JSONDecodeError) → delete it, recompute.
- The write is atomic: write to `<cache_path>.tmp`, then `os.replace()`.

Spec: see docs/plans/20260824-status-line-tokens-aggregation.md (Task 3).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from status_line import compute_main_cum


FIXTURES_DIR = Path(__file__).parent / "fixtures"
MAIN_NORMAL = FIXTURES_DIR / "main_normal.jsonl"
MAIN_TOOL_USE = FIXTURES_DIR / "main_with_tool_use.jsonl"
AGENT_NO_ASSISTANT = FIXTURES_DIR / "agent_no_assistant.jsonl"


# ---------------------------------------------------------------------------
# empty file / no assistant events
# ---------------------------------------------------------------------------

def test_empty_jsonl_returns_zeros(tmp_path: Path) -> None:
    """Empty jsonl file → zeros across the board, no tool_use positions."""
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("")
    cache = tmp_path / "main_x.json"

    result = compute_main_cum(jsonl, cache)

    assert result["total"] == 0
    assert result["cum_in"] == 0
    assert result["cum_out"] == 0
    assert result["cum_cache_create"] == 0
    assert result["cum_cache_read"] == 0
    assert result["tool_use_positions"] == {}
    # last_uuid is "" or None — we pick "" (empty string) per spec.
    assert result["last_uuid"] in ("", None)
    # fresh compute → cache file should exist and be valid JSON
    assert cache.exists()
    assert json.loads(cache.read_text())["total"] == 0


def test_no_assistant_events_returns_empty(tmp_path: Path) -> None:
    """Jsonl that contains only user events → zero totals and empty positions."""
    cache = tmp_path / "main_no_assist.json"
    result = compute_main_cum(AGENT_NO_ASSISTANT, cache)

    assert result["total"] == 0
    assert result["cum_in"] == 0
    assert result["cum_out"] == 0
    assert result["cum_cache_create"] == 0
    assert result["cum_cache_read"] == 0
    assert result["tool_use_positions"] == {}
    # No assistant event exists → last_uuid is empty.
    assert result["last_uuid"] in ("", None)


# ---------------------------------------------------------------------------
# happy path: sum usage from main_normal
# ---------------------------------------------------------------------------

def test_main_normal_sums_usage(tmp_path: Path) -> None:
    """Sum input/output/cache_creation/cache_read across all assistant events
    in main_normal.jsonl.

    main_normal.jsonl has 3 assistant events with usage:
      event 1: input=100, cache_creation=50,  cache_read=200, output=30
      event 2: input=150, cache_creation=100, cache_read=500, output=80
      event 3: input=200, cache_creation=150, cache_read=700, output=120

    Sums:
      cum_in = 450, cum_out = 230, cum_cache_create = 300, cum_cache_read = 1400
      total = 2380
    """
    cache = tmp_path / "main_normal.json"
    result = compute_main_cum(MAIN_NORMAL, cache)

    assert result["cum_in"] == 450
    assert result["cum_out"] == 230
    assert result["cum_cache_create"] == 300
    assert result["cum_cache_read"] == 1400
    assert result["total"] == 2380
    # Last assistant uuid from main_normal.jsonl is the 3rd assistant event.
    assert result["last_uuid"] == "77777777-7777-7777-7777-777777777777"
    # No tool_use blocks in main_normal → empty positions.
    assert result["tool_use_positions"] == {}


# ---------------------------------------------------------------------------
# tool_use_positions
# ---------------------------------------------------------------------------

def test_main_with_tool_use_extracts_positions(tmp_path: Path) -> None:
    """Scan main_with_tool_use.jsonl and verify tool_use_positions map.

    main_with_tool_use.jsonl contains three assistant events, each with a
    tool_use block. The map records each tool_use id → its event index (line
    number, 0-based) in the jsonl file. The SAME id can appear multiple times;
    we keep the FIRST occurrence.
    """
    cache = tmp_path / "main_tool.json"
    result = compute_main_cum(MAIN_TOOL_USE, cache)

    positions = result["tool_use_positions"]
    assert isinstance(positions, dict)
    # Agent_103 appears in the first assistant event (line 2 in the file).
    assert "Agent_103" in positions
    assert "Agent_107" in positions
    # call_xxx appears in the third assistant event.
    assert "call_xxx" in positions

    # Order check: Agent_103 must come before Agent_107 (different assistant
    # events in chronological order). call_xxx comes after both.
    assert positions["Agent_103"] < positions["Agent_107"]
    assert positions["Agent_107"] < positions["call_xxx"]

    # The map should have exactly these three entries — no other tool_use ids.
    assert set(positions.keys()) == {"Agent_103", "Agent_107", "call_xxx"}


# ---------------------------------------------------------------------------
# cache hit
# ---------------------------------------------------------------------------

def test_cache_hit_returns_cached(tmp_path: Path) -> None:
    """Pre-write a cache file whose last_uuid matches the jsonl tail →
    compute_main_cum returns the cached values without re-scanning.

    Verification: write a sentinel value for `total` (999_999_999) that the
    real jsonl could never produce. If the result equals the sentinel, the
    cache was used.
    """
    cache = tmp_path / "main_hit.json"
    sentinel_total = 999_999_999
    sentinel_positions = {"sentinel_tool_id": 0}
    cached = {
        "cum_in": 1,
        "cum_out": 2,
        "cum_cache_create": 3,
        "cum_cache_read": 4,
        "total": sentinel_total,
        "last_uuid": "66666666-6666-6666-6666-666666666666",  # matches main_with_tool_use tail
        "tool_use_positions": sentinel_positions,
    }
    cache.write_text(json.dumps(cached))

    result = compute_main_cum(MAIN_TOOL_USE, cache)

    # If cache was used, these values must match the sentinel.
    assert result["total"] == sentinel_total
    assert result["tool_use_positions"] == sentinel_positions
    assert result["last_uuid"] == "66666666-6666-6666-6666-666666666666"


def test_cache_miss_recomputes(tmp_path: Path) -> None:
    """Pre-write a cache with a STALE last_uuid → recompute from jsonl,
    overwriting the cache."""
    cache = tmp_path / "main_miss.json"
    stale = {
        "cum_in": 0,
        "cum_out": 0,
        "cum_cache_create": 0,
        "cum_cache_read": 0,
        "total": 0,
        "last_uuid": "stale-uuid-from-old-session",
        "tool_use_positions": {},
    }
    cache.write_text(json.dumps(stale))

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Recomputed values from main_normal, not the stale zeros.
    assert result["total"] == 2380
    assert result["cum_in"] == 450
    # Cache file on disk should now reflect fresh values.
    on_disk = json.loads(cache.read_text())
    assert on_disk["total"] == 2380


# ---------------------------------------------------------------------------
# broken cache recovery
# ---------------------------------------------------------------------------

def test_broken_cache_recovered(tmp_path: Path) -> None:
    """Cache file with invalid JSON → function deletes it and recomputes."""
    cache = tmp_path / "main_broken.json"
    cache.write_text("data: not valid json")

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Result is the recomputed values from main_normal.
    assert result["total"] == 2380
    # Cache file was deleted (during the JSONDecodeError branch) and then
    # rewritten with fresh content — content must now be valid JSON.
    assert cache.exists()
    parsed = json.loads(cache.read_text())
    assert parsed["total"] == 2380
    assert parsed["last_uuid"] == "77777777-7777-7777-7777-777777777777"


def test_broken_cache_non_dict_recovered(tmp_path: Path) -> None:
    """Cache file with valid JSON but not a dict (e.g. a list) → function
    deletes it and recomputes (defensive guard)."""
    cache = tmp_path / "main_list.json"
    cache.write_text(json.dumps([1, 2, 3]))

    result = compute_main_cum(MAIN_NORMAL, cache)

    assert result["total"] == 2380
    assert cache.exists()
    parsed = json.loads(cache.read_text())
    assert isinstance(parsed, dict)
    assert parsed["total"] == 2380


# ---------------------------------------------------------------------------
# atomic write — no leftover .tmp
# ---------------------------------------------------------------------------

def test_atomic_write_via_tmp(tmp_path: Path) -> None:
    """After a fresh compute, no `<cache>.tmp` file remains in the data dir."""
    cache = tmp_path / "main_atomic.json"
    compute_main_cum(MAIN_NORMAL, cache)

    # The cache file itself must exist.
    assert cache.exists()
    # And no `.tmp` leftover next to it.
    tmp_file = cache.with_suffix(cache.suffix + ".tmp")
    assert not tmp_file.exists(), f"leftover tmp file: {tmp_file}"
    # Belt-and-braces: list all files in tmp_path and make sure none end with .tmp.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"leftover tmp files: {leftovers}"


# ---------------------------------------------------------------------------
# missing jsonl
# ---------------------------------------------------------------------------

def test_missing_jsonl_returns_zeros(tmp_path: Path) -> None:
    """If jsonl_path does not exist, return zero result and skip the write."""
    jsonl = tmp_path / "does_not_exist.jsonl"
    cache = tmp_path / "main_missing.json"

    result = compute_main_cum(jsonl, cache)

    assert result["total"] == 0
    assert result["cum_in"] == 0
    assert result["cum_out"] == 0
    assert result["cum_cache_create"] == 0
    assert result["cum_cache_read"] == 0
    assert result["tool_use_positions"] == {}
    assert result["last_uuid"] in ("", None)
    # No jsonl → no cache file written (per spec: "Skip the write if jsonl_path
    # doesn't exist").
    assert not cache.exists()
