"""Tests for _write_agents_cache — atomic persistence of per-agent cache.

_write_agents_cache(cache_path, agents) persists the per-agent snapshot
dict to disk atomically via `.tmp` + os.replace(). The cache entry
shape is governed by _AGENT_CACHE_FIELDS — see
tests/test_compute_agent_snapshot.py for the invariant tests.

Failure modes covered here:
- Empty agents list → empty cache dict written.
- Missing `agentId` in an agent → KeyError (callers must always
  populate this — compute_agent_snapshot guarantees it on both the
  cache-hit and cache-miss paths).
- OSError on write → silently swallowed (cache write is non-fatal;
  output is still correct, just slower on the next invocation).
"""
from __future__ import annotations

import json
from pathlib import Path

from status_line import _AGENT_CACHE_FIELDS, _write_agents_cache


def _snapshot(agent_id: str, **overrides) -> dict:
    """Build a snapshot dict that mirrors what compute_agent_snapshot
    returns. agentId is always present; other fields default to safe
    placeholders so individual tests can override just the bits they
    care about."""
    base = {
        "agentId": agent_id,
        "status": "ok",
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cached": 0,
        "description": f"desc-{agent_id}",
        "toolUseId": f"toolu_{agent_id}",
        "last_uuid": f"uuid-{agent_id}",
        "mtime_jsonl": 1.0,
        "mtime_meta": 1.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_writes_per_agent_dict_with_correct_keys(tmp_path: Path) -> None:
    """Two agents → on-disk cache keyed by agentId, each entry holds
    exactly the _AGENT_CACHE_FIELDS set (no extras, no agentId inside)."""
    cache = tmp_path / "agents.json"
    agents = [
        _snapshot("agent-a", tokens_in=10, status="ok"),
        _snapshot("agent-b", tokens_in=20, status="err"),
    ]

    _write_agents_cache(cache, agents)

    on_disk = json.loads(cache.read_text())
    assert set(on_disk.keys()) == {"agent-a", "agent-b"}, (
        f"cache should be keyed by agentId, got: {sorted(on_disk.keys())}"
    )
    for agent_id, entry in on_disk.items():
        assert set(entry.keys()) == set(_AGENT_CACHE_FIELDS), (
            f"entry {agent_id!r} should have exactly _AGENT_CACHE_FIELDS, "
            f"got extras={set(entry) - set(_AGENT_CACHE_FIELDS)}, "
            f"missing={set(_AGENT_CACHE_FIELDS) - set(entry)}"
        )
        # Breakdown fields preserved through the round-trip.
        assert entry["tokens_in"] in (10, 20)


def test_empty_agents_writes_empty_dict(tmp_path: Path) -> None:
    """Empty list → `{}` on disk (no per-agent entries). The cache file
    still exists; next call recomputes."""
    cache = tmp_path / "agents_empty.json"

    _write_agents_cache(cache, [])

    assert cache.exists()
    on_disk = json.loads(cache.read_text())
    assert on_disk == {}


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------

def test_missing_agentid_raises(tmp_path: Path) -> None:
    """compute_agent_snapshot guarantees `agentId` on both cache-hit and
    cache-miss paths. If a caller violates this contract, KeyError is
    surfaced (NOT swallowed) so the bug is visible — main() catches
    Exception and degrades to the fallback header."""
    import pytest

    cache = tmp_path / "agents_broken.json"
    agents = [{"status": "ok", "tokens_in": 1}]  # no agentId

    with pytest.raises(KeyError):
        _write_agents_cache(cache, agents)


def test_oserror_on_write_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    """Cache write failure is non-fatal — output is still correct, just
    slower next invocation. We monkeypatch _atomic_write_json to raise
    OSError and verify _write_agents_cache returns cleanly (no
    exception)."""
    from status_line import _atomic_write_json

    cache = tmp_path / "agents_write_fail.json"

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("status_line._atomic_write_json", boom)

    # Should not raise.
    _write_agents_cache(cache, [_snapshot("agent-a")])


# ---------------------------------------------------------------------------
# atomicity
# ---------------------------------------------------------------------------

def test_no_tmp_file_left_behind(tmp_path: Path) -> None:
    """After a successful write, no `<cache>.tmp` file remains."""
    cache = tmp_path / "agents_atomic.json"

    _write_agents_cache(cache, [_snapshot("agent-a")])

    leftovers = [
        p.name
        for p in tmp_path.iterdir()
        if p.name.endswith(".tmp")
    ]
    assert leftovers == [], f"leftover tmp files: {leftovers}"
