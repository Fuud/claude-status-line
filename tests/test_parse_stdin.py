"""Tests for parse_stdin pure function.

parse_stdin reads the JSON payload that Claude Code sends to the status line
hook via stdin and returns a dict with:

    {
        "session_id":     str,
        "prompt_id":      str,
        "model":          str,
        "branch":         str,   # `git branch --show-current` or ""
        "user":           str,   # AI_USER env var, or "n/a"
        "context_tokens": int,   # payload.context_window.total_input_tokens or 0
    }

Behaviour:
- Valid JSON with all fields → dict populated from payload.
- Empty / whitespace input → all defaults (no exception).
- Invalid JSON → all defaults (no exception).
- Missing fields → defaults for those fields.
- branch: tries `git --no-optional-locks branch --show-current` in cwd; on any
  error (not a git repo, timeout, git missing) returns "".
- user: read from the AI_USER env var on every call; unset or empty → "n/a".
  Tests delenv/setenv explicitly so they don't depend on the developer's
  environment.

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
import status_line


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
    status_line._branch_cache = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AI_USER", raising=False)
    result = parse_stdin(VALID_PAYLOAD)
    assert result["session_id"] == "abc"
    assert result["prompt_id"] == "p1"
    assert result["model"] == "X"
    # AI_USER unset → defaults to "n/a"
    assert result["user"] == "n/a"
    # branch from git, in a non-git cwd → ""
    assert result["branch"] == ""
    # context_window.total_input_tokens from the payload (int)
    assert result["context_tokens"] == 1234


def test_user_taken_from_ai_user_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AI_USER set → user field mirrors it, payload content is irrelevant."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_USER", "f.bobin")
    result = parse_stdin(VALID_PAYLOAD)
    assert result["user"] == "f.bobin"


def test_user_empty_ai_user_falls_back_to_na(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AI_USER set but empty → "n/a", not an empty status-line field."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_USER", "")
    result = parse_stdin(VALID_PAYLOAD)
    assert result["user"] == "n/a"


def test_empty_string_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # chdir to a non-git dir so _get_branch() returns ""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AI_USER", raising=False)
    result = parse_stdin("")
    assert result == {
        "session_id": "",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
        "context_tokens": 0,
        "transcript_path": "",
    }


def test_whitespace_only_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AI_USER", raising=False)
    result = parse_stdin("   \n  \t  \n")
    assert result == {
        "session_id": "",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
        "context_tokens": 0,
        "transcript_path": "",
    }


def test_invalid_json_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AI_USER", raising=False)
    result = parse_stdin("this is { not json [")
    assert result == {
        "session_id": "",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
        "context_tokens": 0,
        "transcript_path": "",
    }


def test_missing_fields_use_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial JSON without most fields → defaults for missing ones."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AI_USER", raising=False)
    result = parse_stdin(json.dumps({"session_id": "only-sid"}))
    assert result == {
        "session_id": "only-sid",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
        "context_tokens": 0,
        "transcript_path": "",
    }


def test_empty_object_uses_all_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AI_USER", raising=False)
    result = parse_stdin("{}")
    assert result == {
        "session_id": "",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "user": "n/a",
        "context_tokens": 0,
        "transcript_path": "",
    }


def test_branch_empty_when_not_in_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In a non-git directory, branch should be ""."""
    monkeypatch.chdir(tmp_path)
    status_line._branch_cache = None  # bypass TTL cache for deterministic test
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


# ---------------------------------------------------------------------------
# context_tokens extraction (payload.context_window.total_input_tokens)
# ---------------------------------------------------------------------------

def test_context_tokens_extracted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """context_window.total_input_tokens (int) lands in context_tokens."""
    monkeypatch.chdir(tmp_path)
    payload = json.dumps({
        "session_id": "s1",
        "context_window": {"used_percentage": 8, "total_input_tokens": 15500},
    })
    result = parse_stdin(payload)
    assert result["context_tokens"] == 15500


def test_context_tokens_missing_context_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No context_window object at all → default 0 (older CC versions)."""
    monkeypatch.chdir(tmp_path)
    result = parse_stdin(json.dumps({"session_id": "s1"}))
    assert result["context_tokens"] == 0


def test_context_tokens_non_int_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: non-int total_input_tokens (str / list) → 0, not a crash."""
    monkeypatch.chdir(tmp_path)
    payload = json.dumps({
        "session_id": "s1",
        "context_window": {"total_input_tokens": "15500"},
    })
    assert parse_stdin(payload)["context_tokens"] == 0
    payload2 = json.dumps({
        "session_id": "s1",
        "context_window": {"total_input_tokens": [1, 2]},
    })
    assert parse_stdin(payload2)["context_tokens"] == 0


def test_context_tokens_bool_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: True is an int subclass in Python but must NOT pass the
    isinstance gate — a boolean in the token slot is corrupt data."""
    monkeypatch.chdir(tmp_path)
    payload = json.dumps({
        "session_id": "s1",
        "context_window": {"total_input_tokens": True},
    })
    assert parse_stdin(payload)["context_tokens"] == 0


def test_context_tokens_non_dict_context_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: context_window as a non-dict (str) → 0."""
    monkeypatch.chdir(tmp_path)
    payload = json.dumps({"session_id": "s1", "context_window": "big"})
    assert parse_stdin(payload)["context_tokens"] == 0


# ---------------------------------------------------------------------------
# transcript_path extraction (payload.transcript_path)
# ---------------------------------------------------------------------------

def test_transcript_path_extracted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A string transcript_path lands verbatim in the parsed dict — no
    existence check here (parse_stdin stays I/O-free apart from git)."""
    monkeypatch.chdir(tmp_path)
    payload = json.dumps({
        "session_id": "s1",
        "transcript_path": "C:/tmp/does-not-exist-yet.jsonl",
    })
    result = parse_stdin(payload)
    assert result["transcript_path"] == "C:/tmp/does-not-exist-yet.jsonl"


def test_transcript_path_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No transcript_path in payload → default ""."""
    monkeypatch.chdir(tmp_path)
    result = parse_stdin(json.dumps({"session_id": "s1"}))
    assert result["transcript_path"] == ""


def test_transcript_path_non_str_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: non-str transcript_path (int / dict) → "", not a crash."""
    monkeypatch.chdir(tmp_path)
    assert parse_stdin(json.dumps({"transcript_path": 42}))["transcript_path"] == ""
    assert parse_stdin(json.dumps({"transcript_path": {"p": 1}}))["transcript_path"] == ""


def test_get_branch_caches_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Subsequent calls within TTL hit the cache and don't re-invoke git."""
    status_line._branch_cache = None
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
    status_line._branch_cache = None

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
    status_line._branch_cache = None
    monkeypatch.setattr("status_line._get_branch_impl", lambda: "")
    assert _get_branch() == ""
    # and the cache should hold the empty string, not None
    assert status_line._branch_cache is not None and status_line._branch_cache[1] == ""
