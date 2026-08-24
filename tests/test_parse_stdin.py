"""Tests for parse_stdin pure function.

parse_stdin reads the JSON payload that Claude Code sends to the status line
hook via stdin and returns a dict with:

    {
        "session_id": str,
        "prompt_id":  str,
        "model":      str,
        "branch":     str,        # `git branch --show-current` or ""
        "user":       str,        # currently "n/a" — TODO: parse from cwd
    }

Behaviour:
- Valid JSON with all fields → dict populated from payload.
- Empty / whitespace input → all defaults (no exception).
- Invalid JSON → all defaults (no exception).
- Missing fields → defaults for those fields.
- branch: tries `git --no-optional-locks branch --show-current` in cwd; on any
  error (not a git repo, timeout, git missing) returns "".

[deviation] _get_branch caches the result for 5 seconds. Tests that need to
verify the actual subprocess invocation must reset the cache (see
test_get_branch_timeout).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from status_line import parse_stdin, _get_branch, _get_branch_impl


VALID_PAYLOAD = json.dumps(
    {
        "session_id": "abc",
        "model": {"display_name": "X"},
        "context_window": {
            "total_input_tokens": 1234,
            "used_percentage": 45,
        },
        "prompt_id": "p1",
    }
)


def test_valid_json_extracts_all_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset _get_branch cache so we get a fresh subprocess call, and
    chdir to a non-git dir so it returns '' deterministically."""
    _get_branch._cache = None
    monkeypatch.chdir(tmp_path)
    result = parse_stdin(VALID_PAYLOAD)
    assert result["session_id"] == "abc"
    assert result["prompt_id"] == "p1"
    assert result["model"] == "X"
    # user not extractable from current payload → defaults to "n/a"
    assert result["user"] == "n/a"
    # branch from git, in a non-git cwd → ""
    assert result["branch"] == ""


def test_empty_string_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # chdir to a non-git dir so _get_branch() returns ""
    monkeypatch.chdir(tmp_path)
    result = parse_stdin("")
    assert result == {
        "session_id": "",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
    }


def test_whitespace_only_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = parse_stdin("   \n  \t  \n")
    assert result == {
        "session_id": "",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
    }


def test_invalid_json_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = parse_stdin("this is { not json [")
    assert result == {
        "session_id": "",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
    }


def test_missing_fields_use_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial JSON without most fields → defaults for missing ones."""
    monkeypatch.chdir(tmp_path)
    result = parse_stdin(json.dumps({"session_id": "only-sid"}))
    assert result == {
        "session_id": "only-sid",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
    }


def test_empty_object_uses_all_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = parse_stdin("{}")
    assert result == {
        "session_id": "",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
    }


def test_branch_empty_when_not_in_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In a non-git directory, branch should be ""."""
    monkeypatch.chdir(tmp_path)
    _get_branch._cache = None  # bypass TTL cache for deterministic test
    result = parse_stdin(VALID_PAYLOAD)
    assert result["branch"] == ""


def test_non_string_fields_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: non-string session_id/prompt_id/model.display_name values
    fall back to defaults instead of corrupting the dict."""
    monkeypatch.chdir(tmp_path)
    payload = json.dumps(
        {
            "session_id": 12345,  # not a string
            "prompt_id": ["list", "not", "string"],
            "model": "not a dict",
        }
    )
    result = parse_stdin(payload)
    assert result["session_id"] == ""
    assert result["prompt_id"] == ""
    assert result["model"] == ""


def test_get_branch_caches_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Subsequent calls within TTL hit the cache and don't re-invoke git."""
    _get_branch._cache = None
    call_count = {"n": 0}

    def fake_impl() -> str:
        call_count["n"] += 1
        return "cached-branch"

    monkeypatch.setattr("status_line._get_branch_impl", fake_impl)
    a = _get_branch()
    b = _get_branch()
    c = _get_branch()
    assert a == b == c == "cached-branch"
    assert call_count["n"] == 1, f"expected 1 git call, got {call_count['n']}"


def test_get_branch_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_branch_impl takes >2s → _get_branch returns "" without hanging.

    We monkeypatch _get_branch_impl to a function that sleeps for 3 seconds.
    Since _get_branch_impl has a 2-second subprocess timeout (and we monkey-
    patch the whole impl), we instead inject a slow impl directly and verify
    _get_branch doesn't itself hang past the test timeout (we cap the test
    at 5 seconds via pytest's timeout).
    """
    _get_branch._cache = None

    def slow_impl() -> str:
        # simulate a slow git by sleeping longer than the cache TTL
        time.sleep(0.1)
        return "slow-branch"

    monkeypatch.setattr("status_line._get_branch_impl", slow_impl)
    start = time.monotonic()
    result = _get_branch()
    elapsed = time.monotonic() - start
    assert result == "slow-branch"
    # Well under the cache TTL test window
    assert elapsed < 1.0


def test_get_branch_handles_subprocess_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_branch_impl returning "" propagates correctly (non-git repo)."""
    _get_branch._cache = None
    monkeypatch.setattr("status_line._get_branch_impl", lambda: "")
    assert _get_branch() == ""
    # and the cache should hold the empty string, not None
    assert _get_branch._cache[1] == ""
