"""Tests for detect_status pure function.

Status priority (highest first):
- err:  last event is assistant AND has any of:
          - `error` field set (non-empty string)
          - `isApiErrorMessage: true`
          - `apiErrorStatus >= 400`
- stop: meta has `stoppedByUser: true` OR last event is type=user with content
        containing "[Request interrupted by user]"
- ok:   last event is assistant with `stop_reason: end_turn` AND no error
- run:  otherwise (mid-flow tool_use, tool_result mid-flow, no assistant events)

detect_status takes (last_event: dict, meta: dict) and returns one of "err",
"stop", "ok", "run".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from status_line import detect_status


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_last_event(jsonl_name: str) -> dict:
    """Read the LAST non-empty line of a fixture jsonl and parse to dict."""
    path = FIXTURES_DIR / jsonl_name
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    return json.loads(lines[-1])


def _load_meta(meta_name: str) -> dict:
    return json.loads((FIXTURES_DIR / meta_name).read_text())


def test_agent_ok_is_ok() -> None:
    last = _load_last_event("agent_ok.jsonl")
    meta = _load_meta("meta_normal.json")
    assert detect_status(last, meta) == "ok"


def test_agent_err_rate_limit_is_err() -> None:
    last = _load_last_event("agent_err_rate_limit.jsonl")
    meta = _load_meta("meta_normal.json")
    assert detect_status(last, meta) == "err"


def test_agent_err_server_error_is_err() -> None:
    last = _load_last_event("agent_err_server_error.jsonl")
    meta = _load_meta("meta_normal.json")
    assert detect_status(last, meta) == "err"


def test_agent_stopped_user_is_stop() -> None:
    last = _load_last_event("agent_stopped_user.jsonl")
    meta = _load_meta("meta_normal.json")
    assert detect_status(last, meta) == "stop"


def test_agent_running_is_run() -> None:
    last = _load_last_event("agent_running.jsonl")
    meta = _load_meta("meta_normal.json")
    assert detect_status(last, meta) == "run"


def test_agent_no_assistant_is_run() -> None:
    """No assistant events → fallback 'run' (mid-flow tool_result only)."""
    last = _load_last_event("agent_no_assistant.jsonl")
    meta = _load_meta("meta_normal.json")
    assert detect_status(last, meta) == "run"


def test_err_takes_priority_over_stopped_by_user() -> None:
    """If last event has an error AND meta says stoppedByUser=true, err wins."""
    last = _load_last_event("agent_err_with_stopped_by_user.jsonl")
    meta = _load_meta("meta_stopped_by_user.json")
    assert detect_status(last, meta) == "err"


def test_stopped_user_jsonl_with_stopped_meta_is_stop() -> None:
    last = _load_last_event("agent_stopped_user.jsonl")
    meta = _load_meta("meta_stopped_by_user.json")
    assert detect_status(last, meta) == "stop"


def test_meta_stopped_by_user_alone_triggers_stop_on_ok_agent() -> None:
    """meta.stoppedByUser=true alone is enough to trigger 'stop', even if
    the last event is a clean assistant end_turn. This protects against
    cases where the agent finished cleanly but the user interrupted the
    parent flow."""
    last = _load_last_event("agent_ok.jsonl")
    meta = _load_meta("meta_stopped_by_user.json")
    assert detect_status(last, meta) == "stop"


def test_is_api_error_message_triggers_err() -> None:
    """Synthetic last event with isApiErrorMessage=true → err even without
    other error fields."""
    last = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "stop_reason": "end_turn",
            "usage": {},
            "isApiErrorMessage": True,
        },
    }
    meta = _load_meta("meta_normal.json")
    assert detect_status(last, meta) == "err"


def test_api_error_status_500_triggers_err() -> None:
    last = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "stop_reason": "end_turn",
            "usage": {},
            "apiErrorStatus": 500,
        },
    }
    meta = _load_meta("meta_normal.json")
    assert detect_status(last, meta) == "err"


# ---------------------------------------------------------------------------
# _is_user_interrupted — coverage for all 3 content-shape branches
# ---------------------------------------------------------------------------

from status_line import _is_user_interrupted


_INTERRUPT = "[Request interrupted by user]"


def test_user_interrupted_string_content() -> None:
    """Content is a plain string containing the marker → True."""
    event = {
        "type": "user",
        "message": {"role": "user", "content": f"hello {_INTERRUPT} goodbye"},
    }
    assert _is_user_interrupted(event) is True


def test_user_interrupted_list_text_block() -> None:
    """Content is a list with a text block containing the marker → True."""
    event = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": f"first part {_INTERRUPT}"},
            ],
        },
    }
    assert _is_user_interrupted(event) is True


def test_user_interrupted_list_content_field() -> None:
    """Content is a list with a block whose `content` field contains marker
    (alternate shape, used by some Claude Code versions)."""
    event = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "content": f"x {_INTERRUPT} y"},
            ],
        },
    }
    assert _is_user_interrupted(event) is True


def test_user_interrupted_tool_result_nested() -> None:
    """Content is a list with a tool_result whose inner content list contains
    a text block with the marker → True (third branch)."""
    event = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "content": [
                        {"type": "text", "text": f"got {_INTERRUPT}"},
                    ],
                },
            ],
        },
    }
    assert _is_user_interrupted(event) is True


def test_user_interrupted_negative_cases() -> None:
    """Content is normal user message → False."""
    event = {
        "type": "user",
        "message": {"role": "user", "content": "just a regular prompt"},
    }
    assert _is_user_interrupted(event) is False


def test_user_interrupted_wrong_type() -> None:
    """Non-user events (e.g. assistant) with the marker → False."""
    event = {
        "type": "assistant",
        "message": {"role": "assistant", "content": _INTERRUPT},
    }
    assert _is_user_interrupted(event) is False


def test_user_interrupted_empty_content() -> None:
    """User event with empty content → False."""
    event = {"type": "user", "message": {"role": "user", "content": ""}}
    assert _is_user_interrupted(event) is False