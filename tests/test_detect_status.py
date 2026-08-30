"""Tests for detect_status pure function.

Status priority (highest first):
- err:  last event is assistant AND has any of:
          - `error` field set (non-empty string)
          - `isApiErrorMessage: true`
          - `apiErrorStatus >= 400`
        …checked BOTH inside `message` (legacy shape) AND at the event
        top level (CC 2.1.224 synthetic error events — see the
        agent_err_top_level fixture, a real 429 death miscategorized as
        "run" before the fix).
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
# top-level error markers (CC 2.1.224 synthetic error events)
# ---------------------------------------------------------------------------


def test_agent_err_top_level_fixture_is_err() -> None:
    """Real-world shape (session 9b7971ff): the synthetic API-error
    assistant event carries error/isApiErrorMessage/apiErrorStatus at the
    EVENT top level — siblings of `message` — with stop_reason
    'stop_sequence' and a zero-usage <synthetic> model. Before the fix
    every check missed and the dead agent rendered as 'run'."""
    last = _load_last_event("agent_err_top_level.jsonl")
    meta = _load_meta("meta_normal.json")
    assert detect_status(last, meta) == "err"


def test_top_level_error_string_triggers_err() -> None:
    last = {
        "type": "assistant",
        "error": "rate_limit",
        "message": {"role": "assistant", "stop_reason": "stop_sequence"},
    }
    assert detect_status(last, {}) == "err"


def test_top_level_is_api_error_message_triggers_err() -> None:
    last = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "message": {"role": "assistant", "stop_reason": "stop_sequence"},
    }
    assert detect_status(last, {}) == "err"


def test_top_level_api_error_status_triggers_err() -> None:
    last = {
        "type": "assistant",
        "apiErrorStatus": 429,
        "message": {"role": "assistant", "stop_reason": "stop_sequence"},
    }
    assert detect_status(last, {}) == "err"


def test_top_level_markers_below_400_do_not_trigger_err() -> None:
    """apiErrorStatus is only an error from 400 up — same rule as the
    message-level shape; a small status must fall through to run (here:
    stop_sequence is not end_turn either)."""
    last = {
        "type": "assistant",
        "apiErrorStatus": 302,
        "message": {"role": "assistant", "stop_reason": "stop_sequence"},
    }
    assert detect_status(last, {}) == "run"


def test_stop_sequence_without_any_marker_stays_run() -> None:
    """Regression guard for the other half of the misclassification: an
    assistant reply ending in stop_sequence WITHOUT error markers is NOT
    err by itself — only the markers (at either level) make it one."""
    last = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": "partial output"}],
        },
    }
    assert detect_status(last, {}) == "run"


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