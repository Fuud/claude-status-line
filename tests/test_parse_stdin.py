"""Tests for parse_stdin pure function.

parse_stdin reads the JSON payload that Claude Code sends to the status line
hook via stdin and returns a dict with:

    {
        "session_id": str,
        "prompt_id":  str,
        "model":      str,
        "branch":     str,        # `git branch --show-current` or ""
        "ctx_k":       int,        # total_input_tokens / 1000
        "used_pct":   int,        # used_percentage (rounded to int)
        "user":       str,        # currently "n/a" — TODO: parse from cwd
    }

Behaviour:
- Valid JSON with all fields → dict populated from payload.
- Empty / whitespace input → all defaults (no exception).
- Invalid JSON → all defaults (no exception).
- Missing fields → defaults for those fields.
- branch: tries `git --no-optional-locks branch --show-current` in cwd; on any
  error (not a git repo, timeout, git missing) returns "".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from status_line import parse_stdin


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


def test_valid_json_extracts_all_fields() -> None:
    result = parse_stdin(VALID_PAYLOAD)
    assert result["session_id"] == "abc"
    assert result["prompt_id"] == "p1"
    assert result["model"] == "X"
    # 1234 / 1000 = 1.234 → 1
    assert result["ctx_k"] == 1
    assert result["used_pct"] == 45
    # user not extractable from current payload → defaults to "n/a"
    assert result["user"] == "n/a"
    # branch from git, but in test cwd (which IS a git repo) we expect
    # a non-empty string OR "" if git fails for any reason
    assert "branch" in result


def test_empty_string_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # chdir to a non-git dir so _get_branch() returns ""
    monkeypatch.chdir(tmp_path)
    result = parse_stdin("")
    assert result == {
        "session_id": "",
        "prompt_id": "",
        "model": "",
        "branch": "",
        "ctx_k": 0,
        "used_pct": 0,
        "user": "n/a",
    }


def test_whitespace_only_returns_defaults() -> None:
    result = parse_stdin("   \n  \t  \n")
    assert result["session_id"] == ""
    assert result["prompt_id"] == ""
    assert result["model"] == ""
    assert result["ctx_k"] == 0
    assert result["used_pct"] == 0
    assert result["user"] == "n/a"


def test_invalid_json_returns_defaults() -> None:
    result = parse_stdin("this is { not json [")
    assert result["session_id"] == ""
    assert result["prompt_id"] == ""
    assert result["model"] == ""
    assert result["ctx_k"] == 0
    assert result["used_pct"] == 0
    assert result["user"] == "n/a"


def test_missing_fields_use_defaults() -> None:
    """Partial JSON without most fields → defaults for missing ones."""
    result = parse_stdin(json.dumps({"session_id": "only-sid"}))
    assert result["session_id"] == "only-sid"
    assert result["prompt_id"] == ""
    assert result["model"] == ""
    assert result["ctx_k"] == 0
    assert result["used_pct"] == 0
    assert result["user"] == "n/a"


def test_empty_object_uses_all_defaults() -> None:
    result = parse_stdin("{}")
    assert result["session_id"] == ""
    assert result["prompt_id"] == ""
    assert result["model"] == ""
    assert result["ctx_k"] == 0
    assert result["used_pct"] == 0
    assert result["user"] == "n/a"


def test_branch_empty_when_not_in_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In a non-git directory, branch should be ""."""
    monkeypatch.chdir(tmp_path)
    result = parse_stdin(VALID_PAYLOAD)
    assert result["branch"] == ""


def test_ctx_k_rounds_correctly() -> None:
    """12345 / 1000 = 12.345 → int truncation gives 12."""
    payload = json.dumps(
        {
            "session_id": "x",
            "context_window": {"total_input_tokens": 12345, "used_percentage": 7},
            "prompt_id": "p",
            "model": {"display_name": "M"},
        }
    )
    result = parse_stdin(payload)
    assert result["ctx_k"] == 12


def test_used_pct_handles_float() -> None:
    payload = json.dumps(
        {
            "session_id": "x",
            "context_window": {"total_input_tokens": 0, "used_percentage": 33.7},
            "prompt_id": "p",
            "model": {"display_name": "M"},
        }
    )
    result = parse_stdin(payload)
    assert result["used_pct"] == 34  # round half away from zero