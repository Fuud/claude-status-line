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

Spec: see docs/plans/20260824-token-breakdown-table.md (Task 2).
After Task 2: the `total` key is no longer in the result dict (it was dead
after the breakdown-table refactor and is fully removed now).
"""
from __future__ import annotations

import json
from pathlib import Path

from status_line import compute_main_cum


FIXTURES_DIR = Path(__file__).parent / "fixtures"
MAIN_NORMAL = FIXTURES_DIR / "main_normal.jsonl"
MAIN_TOOL_USE = FIXTURES_DIR / "main_with_tool_use.jsonl"
MAIN_QUEUE_OPS = FIXTURES_DIR / "main_with_queue_ops.jsonl"
MAIN_DUP_TASK = FIXTURES_DIR / "main_with_duplicate_task_id.jsonl"
MAIN_MISSING_TAGS = FIXTURES_DIR / "main_with_missing_tags.jsonl"

# Last assistant uuid in MAIN_NORMAL — used as the cache-hit key in
# tests that pre-seed the cache file with a known payload.
MAIN_NORMAL_LAST_UUID = "77777777-7777-7777-7777-777777777777"


def _write_main_cache(
    cache_path: Path,
    *,
    cum_in: int,
    cum_out: int,
    cum_cache_create: int,
    cum_cache_read: int,
    total: int | None = None,
    last_uuid: str = MAIN_NORMAL_LAST_UUID,
    mtime_jsonl: float | None = None,
) -> dict:
    """Write a main-cache payload (the same shape compute_main_cum
    writes to disk) and return the dict.

    `total=None` omits the legacy `total` key; pass an int to include it.
    `mtime_jsonl=None` reads the current MAIN_NORMAL mtime so the cache hit
    succeeds (compute_main_cum's cache key is `(last_uuid, mtime_jsonl)`).
    Shared by the "no total key" and "legacy total field" tests so the
    cache-payload literal lives in one place."""
    if mtime_jsonl is None:
        mtime_jsonl = MAIN_NORMAL.stat().st_mtime
    payload: dict = {
        "cum_in": cum_in,
        "cum_out": cum_out,
        "cum_cache_create": cum_cache_create,
        "cum_cache_read": cum_cache_read,
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime_jsonl,
        "tool_use_positions": {},
    }
    if total is not None:
        payload["total"] = total
    cache_path.write_text(json.dumps(payload))
    return payload


# ---------------------------------------------------------------------------
# empty file / no assistant events
# ---------------------------------------------------------------------------

def test_empty_jsonl_returns_zeros(tmp_path: Path) -> None:
    """Empty jsonl file → zeros across the board, no tool_use positions."""
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("")
    cache = tmp_path / "main_x.json"

    result = compute_main_cum(jsonl, cache)

    assert result["cum_in"] == 0
    assert result["cum_out"] == 0
    assert result["cum_cache_create"] == 0
    assert result["cum_cache_read"] == 0
    assert result["tool_use_positions"] == {}
    # last_uuid is "" (empty string) per spec — code never returns None.
    assert result["last_uuid"] == ""
    # fresh compute → cache file should exist and be valid JSON
    assert cache.exists()
    on_disk = json.loads(cache.read_text())
    assert on_disk["cum_in"] == 0
    assert on_disk["cum_out"] == 0


def test_no_assistant_events_returns_empty(tmp_path: Path) -> None:
    """Jsonl that contains only user events → zero cum_* and empty positions."""
    cache = tmp_path / "main_no_assist.json"
    result = compute_main_cum(FIXTURES_DIR / "agent_no_assistant.jsonl", cache)

    assert result["cum_in"] == 0
    assert result["cum_out"] == 0
    assert result["cum_cache_create"] == 0
    assert result["cum_cache_read"] == 0
    assert result["tool_use_positions"] == {}
    # No assistant event exists → last_uuid is "" (empty string).
    assert result["last_uuid"] == ""


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
    """
    cache = tmp_path / "main_normal.json"
    result = compute_main_cum(MAIN_NORMAL, cache)

    assert result["cum_in"] == 450
    assert result["cum_out"] == 230
    assert result["cum_cache_create"] == 300
    assert result["cum_cache_read"] == 1400
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

    Verification: write a sentinel value for `cum_in` (999_999_999) that the
    real jsonl could never produce. If the result equals the sentinel, the
    cache was used.

    Cache key is now (last_uuid, mtime_jsonl) — the cached entry must include
    both for a hit. mtime_jsonl is read from the jsonl on disk; we use its
    current value here so the cache hit succeeds.
    """
    cache = tmp_path / "main_hit.json"
    sentinel_cum_in = 999_999_999
    sentinel_positions = {"sentinel_tool_id": 0}
    cached = {
        "cum_in": sentinel_cum_in,
        "cum_out": 2,
        "cum_cache_create": 3,
        "cum_cache_read": 4,
        "last_uuid": "66666666-6666-6666-6666-666666666666",  # matches main_with_tool_use tail
        "mtime_jsonl": MAIN_TOOL_USE.stat().st_mtime,
        "tool_use_positions": sentinel_positions,
    }
    cache.write_text(json.dumps(cached))

    result = compute_main_cum(MAIN_TOOL_USE, cache)

    # If cache was used, these values must match the sentinel.
    assert result["cum_in"] == sentinel_cum_in
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
        "last_uuid": "stale-uuid-from-old-session",
        "tool_use_positions": {},
    }
    cache.write_text(json.dumps(stale))

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Recomputed values from main_normal, not the stale zeros.
    assert result["cum_in"] == 450
    # Cache file on disk should now reflect fresh values.
    on_disk = json.loads(cache.read_text())
    assert on_disk["cum_in"] == 450
    assert on_disk["cum_out"] == 230


# ---------------------------------------------------------------------------
# broken cache recovery
# ---------------------------------------------------------------------------

def test_broken_cache_recovered(tmp_path: Path) -> None:
    """Cache file with invalid JSON → function deletes it and recomputes."""
    cache = tmp_path / "main_broken.json"
    cache.write_text("data: not valid json")

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Result is the recomputed values from main_normal.
    assert result["cum_in"] == 450
    # Cache file was deleted (during the JSONDecodeError branch) and then
    # rewritten with fresh content — content must now be valid JSON.
    assert cache.exists()
    parsed = json.loads(cache.read_text())
    assert parsed["cum_in"] == 450
    assert parsed["last_uuid"] == "77777777-7777-7777-7777-777777777777"


def test_broken_cache_non_dict_recovered(tmp_path: Path) -> None:
    """Cache file with valid JSON but not a dict (e.g. a list) → function
    deletes it and recomputes (defensive guard)."""
    cache = tmp_path / "main_list.json"
    cache.write_text(json.dumps([1, 2, 3]))

    result = compute_main_cum(MAIN_NORMAL, cache)

    assert result["cum_in"] == 450
    assert cache.exists()
    parsed = json.loads(cache.read_text())
    assert isinstance(parsed, dict)
    assert parsed["cum_in"] == 450


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
    tmp_file = cache.with_name(cache.name + ".tmp")
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

    assert result["cum_in"] == 0
    assert result["cum_out"] == 0
    assert result["cum_cache_create"] == 0
    assert result["cum_cache_read"] == 0
    assert result["tool_use_positions"] == {}
    assert result["last_uuid"] == ""
    # task_notifications should be present (empty dict) and mtime_jsonl == 0.0
    # when the jsonl doesn't exist.
    assert result["task_notifications"] == {}
    assert result["mtime_jsonl"] == 0.0
    # No jsonl → no cache file written (per spec: "Skip the write if jsonl_path
    # doesn't exist").
    assert not cache.exists()


# ---------------------------------------------------------------------------
# task_notifications extraction (queue-operation events)
# ---------------------------------------------------------------------------

def test_task_notifications_mapping(tmp_path: Path) -> None:
    """main_with_queue_ops.jsonl has 4 enqueue events with known statuses
    (completed/killed/failed/running) and one dequeue. The extractor should
    keep only the three with known statuses, mapping completed→"ok",
    killed→"kill", failed→"err". The unknown "running" is excluded; dequeue
    has no content and contributes nothing."""
    cache = tmp_path / "main_q.json"
    result = compute_main_cum(MAIN_QUEUE_OPS, cache)

    tn = result["task_notifications"]
    assert isinstance(tn, dict)
    assert tn == {
        "agent-aaa111": "ok",
        "agent-bbb222": "kill",
        "agent-ccc333": "err",
    }


def test_task_notifications_last_wins(tmp_path: Path) -> None:
    """main_with_duplicate_task_id.jsonl has TWO notifications for the same
    task-id (agent-eee555): first completed, then killed. Last-wins: the
    final dict must show "kill" for that key."""
    cache = tmp_path / "main_dup.json"
    result = compute_main_cum(MAIN_DUP_TASK, cache)

    tn = result["task_notifications"]
    assert tn == {"agent-eee555": "kill"}


def test_task_notifications_unknown_status_skipped(tmp_path: Path) -> None:
    """main_with_queue_ops.jsonl has one enqueue with status="running" —
    an unknown value not in the map. It must NOT be added to the dict."""
    cache = tmp_path / "main_q2.json"
    result = compute_main_cum(MAIN_QUEUE_OPS, cache)

    tn = result["task_notifications"]
    assert "agent-ddd444" not in tn


def test_task_notifications_missing_tags_skipped(tmp_path: Path) -> None:
    """main_with_missing_tags.jsonl has three enqueue events: one with
    task-id but no status, one with status but no task-id, one with no
    task-notification tags at all. None of them should produce a dict entry."""
    cache = tmp_path / "main_mt.json"
    result = compute_main_cum(MAIN_MISSING_TAGS, cache)

    tn = result["task_notifications"]
    assert tn == {}


def test_task_notifications_dequeue_no_content(tmp_path: Path) -> None:
    """The dequeue/remove operations in main_with_queue_ops.jsonl have no
    `content` field — they must be skipped silently."""
    cache = tmp_path / "main_q3.json"
    result = compute_main_cum(MAIN_QUEUE_OPS, cache)

    # Only the 3 enqueue-with-known-status should be in the dict; dequeue
    # and remove contribute nothing.
    assert len(result["task_notifications"]) == 3


def test_task_notifications_empty_when_no_jsonl(tmp_path: Path) -> None:
    """Empty main jsonl → task_notifications is empty dict, NOT a KeyError."""
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("")
    cache = tmp_path / "main_empty.json"

    result = compute_main_cum(jsonl, cache)

    assert result["task_notifications"] == {}
    assert isinstance(result["task_notifications"], dict)


# ---------------------------------------------------------------------------
# mtime_jsonl in cache key
# ---------------------------------------------------------------------------

def test_cache_hit_preserves_task_notifications(tmp_path: Path) -> None:
    """Pre-write a cache whose task_notifications field has a sentinel value
    ({"sentinel-agent": "ok"}). After compute_main_cum is called on a jsonl
    that does NOT contain such a task-id, the cache hit should preserve the
    sentinel — proving the cache-hit branch returns task_notifications
    correctly."""
    cache = tmp_path / "main_hit_tn.json"
    cached = {
        "cum_in": 1,
        "cum_out": 2,
        "cum_cache_create": 3,
        "cum_cache_read": 4,
        "total": 999_999_999,
        "last_uuid": "66666666-6666-6666-6666-666666666666",  # matches MAIN_TOOL_USE tail
        "tool_use_positions": {},
        "mtime_jsonl": MAIN_TOOL_USE.stat().st_mtime,
        "task_notifications": {"sentinel-agent": "ok"},
    }
    cache.write_text(json.dumps(cached))

    result = compute_main_cum(MAIN_TOOL_USE, cache)

    # Cache hit: sentinel values survive.
    assert result["total"] == 999_999_999
    assert result["task_notifications"] == {"sentinel-agent": "ok"}
    assert result["mtime_jsonl"] == MAIN_TOOL_USE.stat().st_mtime


def test_cache_invalidates_on_mtime_change(tmp_path: Path) -> None:
    """Pre-write cache with stale mtime_jsonl (an obviously OLD value).
    When the jsonl's current mtime is newer, the cache must be treated as
    stale → recompute → fresh task_notifications picked up.

    This is the bug the mtime-in-key change is designed to prevent:
    queue-events appended without new assistant-events would leave
    last_uuid unchanged but bump mtime_jsonl; without the mtime in the
    key, the cache would keep returning a stale task_notifications dict.
    """
    # Copy fixture into tmp_path so we control its mtime
    src = MAIN_QUEUE_OPS
    jsonl = tmp_path / "main_for_mtime.jsonl"
    jsonl.write_text(src.read_text())

    # Pre-write cache with an obviously STALE mtime (1.0 — Jan 1970).
    cache = tmp_path / "main_mtime.json"
    stale_cached = {
        "cum_in": 0,
        "cum_out": 0,
        "cum_cache_create": 0,
        "cum_cache_read": 0,
        "total": 0,
        "last_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",  # matches fixture tail
        "tool_use_positions": {},
        "mtime_jsonl": 1.0,  # intentionally stale
        "task_notifications": {"stale-agent": "ok"},  # stale sentinel
    }
    cache.write_text(json.dumps(stale_cached))

    result = compute_main_cum(jsonl, cache)

    # Cache must have been invalidated by the mtime mismatch.
    # Fresh task_notifications (3 entries from fixture) should be present,
    # NOT the stale sentinel.
    assert "stale-agent" not in result["task_notifications"]
    assert "agent-aaa111" in result["task_notifications"]
    assert result["task_notifications"]["agent-aaa111"] == "ok"


def test_cache_hit_preserves_mtime_jsonl_field(tmp_path: Path) -> None:
    """On a cache hit, the returned dict must include mtime_jsonl matching
    the on-disk file. Downstream consumers (and the cache-hit code itself)
    rely on this being populated."""
    cache = tmp_path / "main_mt2.json"
    cached = {
        "cum_in": 0,
        "cum_out": 0,
        "cum_cache_create": 0,
        "cum_cache_read": 0,
        "total": 0,
        "last_uuid": "66666666-6666-6666-6666-666666666666",
        "tool_use_positions": {},
        "mtime_jsonl": MAIN_TOOL_USE.stat().st_mtime,
        "task_notifications": {},
    }
    cache.write_text(json.dumps(cached))

    result = compute_main_cum(MAIN_TOOL_USE, cache)

    assert "mtime_jsonl" in result
    assert result["mtime_jsonl"] == MAIN_TOOL_USE.stat().st_mtime


def test_atomic_write_contains_new_fields(tmp_path: Path) -> None:
    """After a fresh compute, the cached file must include both new fields
    (mtime_jsonl, task_notifications) so that a subsequent cache hit can
    verify the key."""
    cache = tmp_path / "main_full.json"
    compute_main_cum(MAIN_QUEUE_OPS, cache)

    on_disk = json.loads(cache.read_text())
    assert "mtime_jsonl" in on_disk
    assert "task_notifications" in on_disk
    assert on_disk["task_notifications"] == {
        "agent-aaa111": "ok",
        "agent-bbb222": "kill",
        "agent-ccc333": "err",
    }
    assert on_disk["mtime_jsonl"] == MAIN_QUEUE_OPS.stat().st_mtime


# ---------------------------------------------------------------------------
# Task 2: drop `total` from the result dict
# ---------------------------------------------------------------------------

def test_result_has_no_total_key(tmp_path: Path) -> None:
    """compute_main_cum returns a dict that does NOT contain the `total` key.

    After Task 2, `total` is removed: render gets the three breakdown values
    directly (cum_in / cum_out / cum_cache_read) and sums them itself. The
    dead `total` field is gone.
    """
    cache = tmp_path / "main_no_total.json"
    result = compute_main_cum(MAIN_NORMAL, cache)

    assert "total" not in result, (
        f"`total` key should not be present in compute_main_cum result, "
        f"got: {sorted(result.keys())}"
    )


def test_empty_main_result_has_no_total_key(tmp_path: Path) -> None:
    """For a missing jsonl, compute_main_cum returns _EMPTY_MAIN_RESULT (a copy).
    That copy must also NOT contain `total`.
    """
    jsonl = tmp_path / "does_not_exist.jsonl"
    cache = tmp_path / "main_missing_no_total.json"

    result = compute_main_cum(jsonl, cache)

    assert "total" not in result, (
        f"_EMPTY_MAIN_RESULT (returned when jsonl missing) must not have "
        f"`total`, got: {sorted(result.keys())}"
    )
    # Sanity: the other expected keys ARE present.
    assert "cum_in" in result
    assert "cum_out" in result
    assert "cum_cache_create" in result
    assert "cum_cache_read" in result
    assert "last_uuid" in result
    assert "tool_use_positions" in result


def test_cached_payload_has_no_total_key(tmp_path: Path) -> None:
    """The on-disk cache payload (data/main_<sid>.json) must NOT contain
    `total` after Task 2 — both for fresh-compute and cache-hit paths."""
    cache = tmp_path / "main_no_total_disk.json"

    # Fresh compute path.
    result = compute_main_cum(MAIN_NORMAL, cache)
    assert "total" not in result
    on_disk = json.loads(cache.read_text())
    assert "total" not in on_disk, (
        f"`total` must not be persisted to the cache file either, "
        f"got keys: {sorted(on_disk.keys())}"
    )

    # Cache-hit path: a cached entry that LACKS `total` must still be
    # accepted (we don't fail closed on missing `total` because the field
    # is simply absent on the new schema — the old "total present" check
    # would have made every existing live cache invalidate forever).
    _write_main_cache(
        cache,
        cum_in=11,
        cum_out=22,
        cum_cache_create=33,
        cum_cache_read=44,
    )
    result2 = compute_main_cum(MAIN_NORMAL, cache)
    assert result2["cum_in"] == 11  # cache hit succeeded
    assert "total" not in result2


def test_cache_hit_accepts_legacy_total_field(tmp_path: Path) -> None:
    """Pre-upgrade caches from the old schema include a `total` key (cum_in
    + cum_out + cum_cache_create + cum_cache_read). The cache-hit branch
    must ignore that extra field — `total` is not consulted anywhere —
    so the cache still returns the cached cum_* values intact. This is
    the upgrade-path guarantee: an existing live cache with `total`
    continues to feed correct values to render on first run after upgrade,
    until the cache is rewritten by a real recompute.
    """
    cache = tmp_path / "main_with_legacy_total.json"
    cached = _write_main_cache(
        cache,
        cum_in=100,
        cum_out=50,
        cum_cache_create=25,
        cum_cache_read=200,
        total=100 + 50 + 25 + 200,  # legacy field, would be re-derived
    )

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Cache hit: cum_in/out/cache_create/cache_read preserved.
    assert result["cum_in"] == 100
    assert result["cum_out"] == 50
    assert result["cum_cache_create"] == 25
    assert result["cum_cache_read"] == 200
    # The legacy `total` is left untouched on the cached dict (we never
    # strip it — render doesn't read it, and stripping would invalidate
    # every existing live cache on first run after upgrade).
    assert result.get("total") == cached["total"]
