"""Tests for compute_main_cum.

compute_main_cum(jsonl_path, cache_path) reads a main session jsonl and
returns the per-model token breakdown (per_model — the model/cost columns'
data), the first-message breakdown (start_in/start_out/start_cached — the
table's "start:" row), the last assistant event's context occupancy
(context_tokens), plus a map of tool_use ids to their event indices in the
jsonl. Results are cached in `cache_path` keyed by (last assistant uuid,
jsonl mtime) — if neither changed, the cached values are returned without
re-scanning.

Cache semantics:
- If `cache_path` exists, load and compare `last_uuid` + `mtime_jsonl` to
  the jsonl's state. If equal → return cached values.
- If the cache file is malformed (JSONDecodeError) → delete it, recompute.
- The write is atomic: write to `<cache_path>.tmp`, then `os.replace()`.

Spec: see docs/plans/20260824-token-breakdown-table.md (Task 2) and
docs/plans/20260826-status-line-model-cost-columns.md (Task 2).
Removed fields: `total` (breakdown-table refactor) and the flat
`cum_in`/`cum_out`/`cum_cache_create`/`cum_cache_read` sums (model-columns
refactor — render derives group totals from per_model). Both may still
appear in PRE-upgrade cache files; the cache-hit path ignores extra keys.
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
    start_in: int = 0,
    start_out: int = 0,
    start_cached: int = 0,
    context_tokens: int = 0,
    total: int | None = None,
    per_model: dict | None = None,
    last_uuid: str = MAIN_NORMAL_LAST_UUID,
    mtime_jsonl: float | None = None,
    extra: dict | None = None,
) -> dict:
    """Write a main-cache payload (the same shape compute_main_cum
    writes to disk) and return the dict.

    `total=None` omits the legacy `total` key; pass an int to include it.
    `context_tokens` and the `start_*` fields default to 0 — PRESENT
    (possibly zero) fields; the cache-hit guard checks presence, not value.
    Same for `per_model` (defaults to a present-but-arbitrary dict).
    `mtime_jsonl=None` reads the current MAIN_NORMAL mtime so the cache hit
    succeeds (compute_main_cum's cache key is `(last_uuid, mtime_jsonl)`).
    `extra` merges additional (legacy) keys into the payload — used to
    prove the cache-hit path tolerates pre-upgrade fields.
    Shared by the "no total key" and "legacy total field" tests so the
    cache-payload literal lives in one place."""
    if mtime_jsonl is None:
        mtime_jsonl = MAIN_NORMAL.stat().st_mtime
    payload: dict = {
        "start_in": start_in,
        "start_out": start_out,
        "start_cached": start_cached,
        "context_tokens": context_tokens,
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime_jsonl,
        "tool_use_positions": {},
        "per_model": per_model if per_model is not None else {"sentinel-model": {"in": 1, "out": 2, "cached": 3}},
    }
    if total is not None:
        payload["total"] = total
    if extra:
        payload.update(extra)
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

    assert result["per_model"] == {}
    assert result["tool_use_positions"] == {}
    # last_uuid is "" (empty string) per spec — code never returns None.
    assert result["last_uuid"] == ""
    # No assistant events → no context occupancy.
    assert result["context_tokens"] == 0
    # fresh compute → cache file should exist and be valid JSON
    assert cache.exists()
    on_disk = json.loads(cache.read_text())
    assert on_disk["per_model"] == {}
    assert on_disk["context_tokens"] == 0


def test_no_assistant_events_returns_empty(tmp_path: Path) -> None:
    """Jsonl that contains only user events → empty per_model/positions."""
    cache = tmp_path / "main_no_assist.json"
    result = compute_main_cum(FIXTURES_DIR / "agent_no_assistant.jsonl", cache)

    assert result["per_model"] == {}
    assert result["tool_use_positions"] == {}
    # No assistant event exists → last_uuid is "" (empty string).
    assert result["last_uuid"] == ""
    assert result["context_tokens"] == 0


# ---------------------------------------------------------------------------
# happy path: sum usage from main_normal
# ---------------------------------------------------------------------------

def test_main_normal_sums_usage(tmp_path: Path) -> None:
    """Per-model sums over all assistant events in main_normal.jsonl.

    main_normal.jsonl has 3 assistant events with usage (all model
    "claude-opus-4-1"):
      event 1: input=100, cache_creation=50,  cache_read=200, output=30
      event 2: input=150, cache_creation=100, cache_read=500, output=80
      event 3: input=200, cache_creation=150, cache_read=700, output=120

    per_model (cache_read only — cache_creation is never surfaced):
      in = 450, out = 230, cached = 1400
    """
    cache = tmp_path / "main_normal.json"
    result = compute_main_cum(MAIN_NORMAL, cache)

    assert result["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }
    # Last assistant uuid from main_normal.jsonl is the 3rd assistant event.
    assert result["last_uuid"] == "77777777-7777-7777-7777-777777777777"
    # No tool_use blocks in main_normal → empty positions.
    assert result["tool_use_positions"] == {}
    # Context occupancy at the LAST assistant event (200+150+700), not a
    # cumulative sum — feeds the header's "Context: NK (P%)" field.
    assert result["context_tokens"] == 1050


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

    Verification: write sentinel values (per_model context=424_242,
    start_in=434_343, ...) that the real jsonl could never produce. If the
    result equals the sentinels, the cache was used.

    Cache key is now (last_uuid, mtime_jsonl) — the cached entry must include
    both for a hit. mtime_jsonl is read from the jsonl on disk; we use its
    current value here so the cache hit succeeds.
    """
    cache = tmp_path / "main_hit.json"
    sentinel_positions = {"sentinel_tool_id": 0}
    sentinel_context = 424_242
    sentinel_start = 434_343
    sentinel_per_model = {"sentinel-model": {"in": 7, "out": 8, "cached": 9}}
    cached = {
        "start_in": sentinel_start,
        "start_out": 0,
        "start_cached": 0,
        "context_tokens": sentinel_context,
        "last_uuid": "66666666-6666-6666-6666-666666666666",  # matches main_with_tool_use tail
        "mtime_jsonl": MAIN_TOOL_USE.stat().st_mtime,
        "tool_use_positions": sentinel_positions,
        "per_model": sentinel_per_model,
    }
    cache.write_text(json.dumps(cached))

    result = compute_main_cum(MAIN_TOOL_USE, cache)

    # If cache was used, these values must match the sentinel.
    assert result["tool_use_positions"] == sentinel_positions
    assert result["context_tokens"] == sentinel_context
    assert result["start_in"] == sentinel_start
    assert result["per_model"] == sentinel_per_model
    assert result["last_uuid"] == "66666666-6666-6666-6666-666666666666"


def test_cache_miss_recomputes(tmp_path: Path) -> None:
    """Pre-write a cache with a STALE last_uuid → recompute from jsonl,
    overwriting the cache."""
    cache = tmp_path / "main_miss.json"
    stale = {
        "start_in": 0,
        "start_out": 0,
        "start_cached": 0,
        "context_tokens": 0,
        "last_uuid": "stale-uuid-from-old-session",
        "tool_use_positions": {},
        "per_model": {},
    }
    cache.write_text(json.dumps(stale))

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Recomputed values from main_normal, not the stale zeros.
    assert result["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }
    # Cache file on disk should now reflect fresh values.
    on_disk = json.loads(cache.read_text())
    assert on_disk["per_model"] == result["per_model"]
    assert on_disk["context_tokens"] == 1050


# ---------------------------------------------------------------------------
# broken cache recovery
# ---------------------------------------------------------------------------

def test_broken_cache_recovered(tmp_path: Path) -> None:
    """Cache file with invalid JSON → function deletes it and recomputes."""
    cache = tmp_path / "main_broken.json"
    cache.write_text("data: not valid json")

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Result is the recomputed values from main_normal.
    assert result["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }
    # Cache file was deleted (during the JSONDecodeError branch) and then
    # rewritten with fresh content — content must now be valid JSON.
    assert cache.exists()
    parsed = json.loads(cache.read_text())
    assert parsed["per_model"] == result["per_model"]
    assert parsed["last_uuid"] == "77777777-7777-7777-7777-777777777777"


def test_broken_cache_non_dict_recovered(tmp_path: Path) -> None:
    """Cache file with valid JSON but not a dict (e.g. a list) → function
    deletes it and recomputes (defensive guard)."""
    cache = tmp_path / "main_list.json"
    cache.write_text(json.dumps([1, 2, 3]))

    result = compute_main_cum(MAIN_NORMAL, cache)

    assert result["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }
    assert cache.exists()
    parsed = json.loads(cache.read_text())
    assert isinstance(parsed, dict)
    assert parsed["per_model"] == result["per_model"]


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

    assert result["per_model"] == {}
    assert result["tool_use_positions"] == {}
    assert result["last_uuid"] == ""
    # task_notifications should be present (empty dict) and mtime_jsonl == 0.0
    # when the jsonl doesn't exist.
    assert result["task_notifications"] == {}
    assert result["mtime_jsonl"] == 0.0
    assert result["context_tokens"] == 0
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
        "start_in": 0,
        "start_out": 0,
        "start_cached": 0,
        "context_tokens": 0,
        "total": 999_999_999,
        "last_uuid": "66666666-6666-6666-6666-666666666666",  # matches MAIN_TOOL_USE tail
        "tool_use_positions": {},
        "mtime_jsonl": MAIN_TOOL_USE.stat().st_mtime,
        "task_notifications": {"sentinel-agent": "ok"},
        "per_model": {"sentinel-model": {"in": 1, "out": 1, "cached": 1}},
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
        "start_in": 0,
        "start_out": 0,
        "start_cached": 0,
        "context_tokens": 0,
        "last_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",  # matches fixture tail
        "tool_use_positions": {},
        "mtime_jsonl": 1.0,  # intentionally stale
        "task_notifications": {"stale-agent": "ok"},  # stale sentinel
        "per_model": {},
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
        "start_in": 0,
        "start_out": 0,
        "start_cached": 0,
        "context_tokens": 0,
        "last_uuid": "66666666-6666-6666-6666-666666666666",
        "tool_use_positions": {},
        "mtime_jsonl": MAIN_TOOL_USE.stat().st_mtime,
        "task_notifications": {},
        "per_model": {},
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
# Task 2: drop `total` from the result dict; review follow-up: drop the flat
# cum_* sums too (render derives group totals from per_model)
# ---------------------------------------------------------------------------

def test_result_has_no_total_key(tmp_path: Path) -> None:
    """compute_main_cum returns a dict that does NOT contain the `total` key.

    After Task 2, `total` is removed: render sums the breakdown values
    itself. The dead `total` field is gone.
    """
    cache = tmp_path / "main_no_total.json"
    result = compute_main_cum(MAIN_NORMAL, cache)

    assert "total" not in result, (
        f"`total` key should not be present in compute_main_cum result, "
        f"got: {sorted(result.keys())}"
    )


def test_result_has_no_flat_cum_keys(tmp_path: Path) -> None:
    """[review follow-up] The flat cum_in / cum_out / cum_cache_create /
    cum_cache_read sums were removed together with the model columns —
    their last production reader was the render refactor. per_model now
    carries the same information (partitioned by model)."""
    cache = tmp_path / "main_no_cum.json"
    result = compute_main_cum(MAIN_NORMAL, cache)

    for key in ("cum_in", "cum_out", "cum_cache_create", "cum_cache_read"):
        assert key not in result, (
            f"`{key}` should not be present in compute_main_cum result, "
            f"got: {sorted(result.keys())}"
        )
        on_disk = json.loads(cache.read_text())
        assert key not in on_disk, (
            f"`{key}` must not be persisted to the cache either, "
            f"got keys: {sorted(on_disk.keys())}"
        )
    # Sanity: the surviving keys ARE present.
    for key in (
        "start_in", "start_out", "start_cached", "context_tokens",
        "last_uuid", "mtime_jsonl", "tool_use_positions",
        "task_notifications", "per_model",
    ):
        assert key in result, f"`{key}` missing from result: {sorted(result.keys())}"


def test_empty_main_result_has_no_total_key(tmp_path: Path) -> None:
    """For a missing jsonl, compute_main_cum returns _EMPTY_MAIN_RESULT (a copy).
    That copy must also NOT contain `total` or the removed cum_* keys.
    """
    jsonl = tmp_path / "does_not_exist.jsonl"
    cache = tmp_path / "main_missing_no_total.json"

    result = compute_main_cum(jsonl, cache)

    assert "total" not in result, (
        f"_EMPTY_MAIN_RESULT (returned when jsonl missing) must not have "
        f"`total`, got: {sorted(result.keys())}"
    )
    assert not any(k.startswith("cum_") for k in result), (
        f"_EMPTY_MAIN_RESULT must not carry removed cum_* keys, "
        f"got: {sorted(result.keys())}"
    )
    # Sanity: the other expected keys ARE present.
    assert "last_uuid" in result
    assert "tool_use_positions" in result
    assert "per_model" in result


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
    _write_main_cache(cache)
    result2 = compute_main_cum(MAIN_NORMAL, cache)
    assert result2["per_model"]["sentinel-model"]["in"] == 1  # cache hit
    assert "total" not in result2


def test_cache_hit_accepts_legacy_total_field(tmp_path: Path) -> None:
    """Pre-upgrade caches from the old schema include a `total` key (and,
    after this branch, the removed cum_* sums). The cache-hit branch must
    ignore those extra fields — none is consulted anywhere — so the cache
    still returns the cached values intact. This is the upgrade-path
    guarantee: an existing live cache continues to feed correct values to
    render on first run after upgrade, until the cache is rewritten by a
    real recompute.
    """
    cache = tmp_path / "main_with_legacy_total.json"
    cached = _write_main_cache(
        cache,
        total=375,  # legacy field, would be re-derived
        extra={
            "cum_in": 100,
            "cum_out": 50,
            "cum_cache_create": 25,
            "cum_cache_read": 200,
        },
    )

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Cache hit: the live fields come back from the cache...
    assert result["per_model"] == cached["per_model"]
    # ...and the legacy extras are left untouched on the cached dict (we
    # never strip them — render doesn't read them, and stripping would
    # invalidate every existing live cache on first run after upgrade).
    assert result.get("total") == 375
    assert result.get("cum_in") == 100
    assert result.get("cum_cache_read") == 200


# ---------------------------------------------------------------------------
# context_tokens (header "Context:" field source, jsonl fallback)
# ---------------------------------------------------------------------------

def test_context_tokens_is_last_assistant_not_cumulative(tmp_path: Path) -> None:
    """context_tokens must reflect the LAST assistant event's occupancy,
    not a cumulative sum. main_normal's 3rd event: 200+150+700 = 1050
    (while the per-model sums over all events are in=450, cached=1400)."""
    cache = tmp_path / "main_ctx.json"
    result = compute_main_cum(MAIN_NORMAL, cache)

    assert result["context_tokens"] == 1050
    rec = result["per_model"]["claude-opus-4-1"]
    assert result["context_tokens"] != rec["in"] + rec["cached"]
    # Persisted to the cache file too.
    on_disk = json.loads(cache.read_text())
    assert on_disk["context_tokens"] == 1050


def test_context_tokens_assistant_without_usage(tmp_path: Path) -> None:
    """An assistant event with NO usage block contributes nothing — the
    running context value stays at the previous event's occupancy."""
    jsonl = tmp_path / "no_usage.jsonl"
    jsonl.write_text(
        '{"type":"assistant","message":{"content":[],"usage":{"input_tokens":100,'
        '"cache_creation_input_tokens":20,"cache_read_input_tokens":30,"output_tokens":1}},'
        '"uuid":"u1"}\n'
        '{"type":"assistant","message":{"content":[]},"uuid":"u2"}\n'
    )
    cache = tmp_path / "main_no_usage.json"
    result = compute_main_cum(jsonl, cache)

    # Last assistant event has no usage → context stays at the earlier
    # event's 150 (100+20+30); uuid tracks u2.
    assert result["context_tokens"] == 150
    assert result["last_uuid"] == "u2"


def test_cache_hit_requires_context_tokens_field(tmp_path: Path) -> None:
    """Pre-upgrade cache shape: both key parts match but the dict LACKS
    context_tokens. The field-presence guard must treat it as a MISS and
    recompute (else the header would render "0K (0%)" for one cycle after
    upgrade). Mirrors the agents-cache breakdown-fields guard."""
    cache = tmp_path / "main_old_schema.json"
    # Intentionally WITHOUT "context_tokens".
    cached = {
        "start_in": 0,
        "start_out": 0,
        "start_cached": 0,
        "last_uuid": MAIN_NORMAL_LAST_UUID,
        "mtime_jsonl": MAIN_NORMAL.stat().st_mtime,
        "tool_use_positions": {},
        "task_notifications": {},
        "per_model": {"stale-model": {"in": 111, "out": 222, "cached": 333}},
    }
    cache.write_text(json.dumps(cached))

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Recomputed, not the stale sentinels.
    assert result["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }
    assert result["context_tokens"] == 1050
    # Cache rewritten in the new shape.
    on_disk = json.loads(cache.read_text())
    assert on_disk["context_tokens"] == 1050


# ---------------------------------------------------------------------------
# start_* fields (first-message breakdown, the table's "start:" row)
# ---------------------------------------------------------------------------

def test_start_values_from_first_assistant_event(tmp_path: Path) -> None:
    """start_in/start_out/start_cached mirror the FIRST assistant event's
    usage: main_normal event 1 has input=100, output=30, cache_read=200
    (NOT the cumulative sums, NOT the last event)."""
    cache = tmp_path / "main_start.json"
    result = compute_main_cum(MAIN_NORMAL, cache)

    assert result["start_in"] == 100
    assert result["start_out"] == 30
    assert result["start_cached"] == 200
    # Distinct from both the whole-session per-model sums (450/230/1400)
    # and the last event's occupancy (context_tokens=1050).
    rec = result["per_model"]["claude-opus-4-1"]
    assert result["start_in"] != rec["in"]
    assert result["start_out"] != rec["out"]
    assert result["start_cached"] != rec["cached"]


def test_start_persisted_to_cache(tmp_path: Path) -> None:
    """After a fresh compute, the cache file on disk carries the start_*
    fields so a subsequent cache-hit returns them without re-scanning."""
    cache = tmp_path / "main_start_disk.json"
    compute_main_cum(MAIN_NORMAL, cache)

    on_disk = json.loads(cache.read_text())
    assert on_disk["start_in"] == 100
    assert on_disk["start_out"] == 30
    assert on_disk["start_cached"] == 200

    # Second call hits the cache and returns the same start values.
    result2 = compute_main_cum(MAIN_NORMAL, cache)
    assert result2["start_in"] == 100
    assert result2["start_out"] == 30
    assert result2["start_cached"] == 200


def test_start_zero_when_no_assistant_events(tmp_path: Path) -> None:
    """Empty jsonl (or one with no assistant events) → start_* are zeros,
    so the table's start row renders "0 0 0" for a fresh session."""
    jsonl = tmp_path / "empty.jsonl"
    jsonl.write_text("")
    cache = tmp_path / "main_start_empty.json"

    result = compute_main_cum(jsonl, cache)

    assert result["start_in"] == 0
    assert result["start_out"] == 0
    assert result["start_cached"] == 0


def test_start_first_event_without_usage_skipped(tmp_path: Path) -> None:
    """A leading assistant event with NO usage block contributes nothing —
    the start triple is captured from the FIRST assistant event that HAS
    usage, mirroring the context_tokens behavior
    (test_context_tokens_assistant_without_usage)."""
    jsonl = tmp_path / "start_no_usage.jsonl"
    jsonl.write_text(
        '{"type":"assistant","message":{"content":[]},"uuid":"u1"}\n'
        '{"type":"assistant","message":{"content":[],"usage":{"input_tokens":100,'
        '"cache_creation_input_tokens":20,"cache_read_input_tokens":30,"output_tokens":40}},'
        '"uuid":"u2"}\n'
    )
    cache = tmp_path / "main_start_nu.json"
    result = compute_main_cum(jsonl, cache)

    assert result["start_in"] == 100
    assert result["start_out"] == 40
    assert result["start_cached"] == 30


def test_cache_hit_requires_start_fields(tmp_path: Path) -> None:
    """Pre-start-row cache shape: both key parts match but the dict LACKS
    the start_* fields. The field-presence guard must treat it as a MISS
    and recompute (else the start row would render zeros for one cycle
    after upgrade). Mirrors the context_tokens guard test above."""
    cache = tmp_path / "main_old_schema_no_start.json"
    # Intentionally WITHOUT the start_* fields (but WITH context_tokens,
    # so only the start guard can trigger the miss).
    cached = {
        "context_tokens": 1050,
        "last_uuid": MAIN_NORMAL_LAST_UUID,
        "mtime_jsonl": MAIN_NORMAL.stat().st_mtime,
        "tool_use_positions": {},
        "task_notifications": {},
        "per_model": {"stale-model": {"in": 111, "out": 222, "cached": 333}},
    }
    cache.write_text(json.dumps(cached))

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Recomputed, not the stale sentinels.
    assert result["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }
    assert result["start_in"] == 100
    assert result["start_out"] == 30
    assert result["start_cached"] == 200
    # Cache rewritten in the new shape.
    on_disk = json.loads(cache.read_text())
    assert on_disk["start_in"] == 100


# ---------------------------------------------------------------------------
# per_model accumulation (plan 20260826-status-line-model-cost-columns, Task 2)
# ---------------------------------------------------------------------------

def test_per_model_single_model(tmp_path: Path) -> None:
    """All assistant events share one model → per_model has a single entry
    with the per-model sums of in/out/cached (cache_read; cache_creation is
    never surfaced, matching the cached-column semantics).

    main_normal.jsonl: 3 assistant events, all model "claude-opus-4-1":
      in  = 100+150+200 = 450, out = 30+80+120 = 230, cached = 200+500+700 = 1400
    """
    cache = tmp_path / "main_pm.json"
    result = compute_main_cum(MAIN_NORMAL, cache)

    assert result["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }


def test_per_model_model_change_mid_jsonl(tmp_path: Path) -> None:
    """Model switch mid-session → one per_model entry per model, each summing
    only its own events. Key order follows FIRST APPEARANCE in the scan
    (render relies on dict insertion order for the model-row order)."""
    jsonl = tmp_path / "multi_model.jsonl"
    jsonl.write_text(
        '{"type":"assistant","message":{"model":"glm-5.3","usage":{"input_tokens":10,'
        '"cache_creation_input_tokens":5,"cache_read_input_tokens":100,"output_tokens":2}},'
        '"uuid":"u1"}\n'
        '{"type":"assistant","message":{"model":"kimi-k3","usage":{"input_tokens":20,'
        '"cache_creation_input_tokens":1,"cache_read_input_tokens":200,"output_tokens":4}},'
        '"uuid":"u2"}\n'
        '{"type":"assistant","message":{"model":"glm-5.3","usage":{"input_tokens":1,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":1}},'
        '"uuid":"u3"}\n'
    )
    cache = tmp_path / "main_pm_multi.json"
    result = compute_main_cum(jsonl, cache)

    per_model = result["per_model"]
    # glm-5.3 sums events u1+u3; kimi-k3 sums u2. cache_creation (5/1/0)
    # is NOT part of any per-model record.
    assert per_model == {
        "glm-5.3": {"in": 11, "out": 3, "cached": 100},
        "kimi-k3": {"in": 20, "out": 4, "cached": 200},
    }
    # First-appearance order: glm-5.3 was seen before kimi-k3.
    assert list(per_model.keys()) == ["glm-5.3", "kimi-k3"]


def test_per_model_only_synthetic_keeps_zero_record(tmp_path: Path) -> None:
    """A jsonl whose assistant events are all <synthetic> (zero usage) keeps
    the zero-valued record in per_model. Filtering of zero-token rows is a
    RENDER concern; the scan must not lose the knowledge that the model
    occurred at all."""
    jsonl = tmp_path / "synthetic_only.jsonl"
    jsonl.write_text(
        '{"type":"assistant","message":{"model":"<synthetic>","usage":{"input_tokens":0,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0}},'
        '"uuid":"u1"}\n'
        '{"type":"assistant","message":{"model":"<synthetic>","usage":{"input_tokens":0,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0}},'
        '"uuid":"u2"}\n'
    )
    cache = tmp_path / "main_pm_synth.json"
    result = compute_main_cum(jsonl, cache)

    assert result["per_model"] == {
        "<synthetic>": {"in": 0, "out": 0, "cached": 0}
    }


def test_per_model_assistant_without_usage_not_recorded(tmp_path: Path) -> None:
    """An assistant event with NO usage block contributes nothing to
    per_model (same gate as the cum_* sums) — no model entry, not even a
    zero record, because the scan cannot even know tokens existed."""
    jsonl = tmp_path / "no_usage_model.jsonl"
    jsonl.write_text(
        '{"type":"assistant","message":{"model":"glm-5.3","content":[]},"uuid":"u1"}\n'
    )
    cache = tmp_path / "main_pm_nu.json"
    result = compute_main_cum(jsonl, cache)

    assert result["per_model"] == {}


def test_per_model_persisted_to_cache(tmp_path: Path) -> None:
    """Fresh compute persists per_model to the cache file; a second call
    (cache hit) returns it without re-scanning."""
    cache = tmp_path / "main_pm_disk.json"
    compute_main_cum(MAIN_NORMAL, cache)

    on_disk = json.loads(cache.read_text())
    assert on_disk["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }

    result2 = compute_main_cum(MAIN_NORMAL, cache)
    assert result2["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }


def test_cache_hit_requires_per_model_field(tmp_path: Path) -> None:
    """Pre-model-column cache shape: both key parts match AND the dict has
    context_tokens + start_*, but LACKS per_model. The field-presence guard
    must treat it as a MISS and recompute (else the model/cost columns would
    render from an empty dict for one cycle after upgrade). Mirrors the
    context_tokens / start_* guard tests above."""
    cache = tmp_path / "main_old_schema_no_pm.json"
    # Intentionally WITHOUT "per_model" (but WITH context_tokens and the
    # start_* fields, so only the per_model guard can trigger the miss).
    cached = {
        "start_in": 100,
        "start_out": 30,
        "start_cached": 200,
        "context_tokens": 1050,
        "last_uuid": MAIN_NORMAL_LAST_UUID,
        "mtime_jsonl": MAIN_NORMAL.stat().st_mtime,
        "tool_use_positions": {},
        "task_notifications": {},
    }
    cache.write_text(json.dumps(cached))

    result = compute_main_cum(MAIN_NORMAL, cache)

    # Recomputed, not the stale sentinels.
    assert result["per_model"] == {
        "claude-opus-4-1": {"in": 450, "out": 230, "cached": 1400}
    }
    # Cache rewritten in the new shape.
    on_disk = json.loads(cache.read_text())
    assert on_disk["per_model"] == result["per_model"]


def test_missing_jsonl_per_model_empty(tmp_path: Path) -> None:
    """Missing jsonl → the _EMPTY_MAIN_RESULT copy must carry an (empty)
    per_model dict — the orchestrator must not KeyError on the race path
    where the jsonl disappears between the existence check and the scan."""
    jsonl = tmp_path / "does_not_exist.jsonl"
    cache = tmp_path / "main_pm_missing.json"

    result = compute_main_cum(jsonl, cache)

    assert result["per_model"] == {}
    assert isinstance(result["per_model"], dict)


# ---------------------------------------------------------------------------
# malformed token values (review follow-up: the forward scans used to raise
# ValueError out of int('abc') on ANY corrupt usage value, degrading the
# whole status line to the fallback header via main()'s catch-all)
# ---------------------------------------------------------------------------

def test_malformed_token_values_coerce_to_zero_not_raise(tmp_path: Path) -> None:
    """A corrupt jsonl whose usage fields are non-numeric strings must not
    raise out of compute_main_cum (the module invariant "never raises"):
    _to_int coerces each bad value to 0 and the scan keeps going."""
    jsonl = tmp_path / "bad_tokens.jsonl"
    jsonl.write_text(
        '{"type":"assistant","message":{"model":"glm-5.3","usage":'
        '{"input_tokens":"abc","output_tokens":3,'
        '"cache_creation_input_tokens":null,"cache_read_input_tokens":"7"}},'
        '"uuid":"u1"}\n'
    )
    cache = tmp_path / "main_bad_tokens.json"

    result = compute_main_cum(jsonl, cache)  # must not raise

    assert result["per_model"] == {
        "glm-5.3": {"in": 0, "out": 3, "cached": 7}
    }
    # context = in(0) + cache_creation(None→0) + cache_read(7)
    assert result["context_tokens"] == 7
    assert result["start_in"] == 0
    assert result["start_cached"] == 7


def test_assistant_event_without_model_field_uses_empty_key(tmp_path: Path) -> None:
    """An assistant event WITH usage but NO model field accumulates under
    the "" key (model = str(msg.get("model") or "")) — the render layer
    then shows the tokens with empty model/cost cells."""
    jsonl = tmp_path / "no_model.jsonl"
    jsonl.write_text(
        '{"type":"assistant","message":{"usage":{"input_tokens":100,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":5,'
        '"output_tokens":10}},"uuid":"u1"}\n'
    )
    cache = tmp_path / "main_no_model.json"

    result = compute_main_cum(jsonl, cache)

    assert result["per_model"] == {"": {"in": 100, "out": 10, "cached": 5}}
