"""Tests for main-scan time segmentation
(plan 20260827-status-line-time-columns, Task 3).

_scan_main_jsonl splits a session into TURNS keyed on "real" user events
(type=user, message.content is a string — prompts, commands, interrupts);
list-content user events (tool_results) are activity, not boundaries. Each
turn becomes a list of [start, end] epoch sub-intervals; AskUserQuestion
pauses split a turn's sub-intervals and an unresolved QA trims the tail.
Three new result fields:

    time_first_ts — epoch of the first event carrying a timestamp of ANY
        type (0.0 when no event has one)
    time_turns    — list (per turn) of lists of [start, end] sub-intervals;
        a turn without any activity is [[u, u]] (degenerate markers are
        dropped later by union_work)
    time_open     — whether the LAST, still-live turn should be extended
        to now by the orchestrator

All fixtures are built inline with the small helpers below; expectations
are derived through status_line._parse_ts so no epoch number is ever
hard-coded.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from status_line import _parse_ts, _scan_main_jsonl

_BASE = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)


def _iso(offset: float) -> str:
    """ISO-8601 Z stamp `offset` seconds after the fixture base."""
    dt = _BASE + timedelta(seconds=offset)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ep(offset: float) -> float:
    """Epoch expectation for fixture offset `offset` (via _parse_ts)."""
    return _parse_ts(_iso(offset))


def _line(**event: object) -> str:
    return json.dumps(event)


def _user_prompt(offset: float, text: str = "do the thing") -> str:
    return _line(
        type="user",
        timestamp=_iso(offset),
        message={"role": "user", "content": text},
        uuid=f"u{offset:.0f}",
    )


def _interrupt(offset: float) -> str:
    return _user_prompt(offset, "[Request interrupted by user]")


def _assistant(
    offset: float, stop: str = "end_turn", *, qa: bool = False, with_ts: bool = True
) -> str:
    if qa:
        content: list = [
            {
                "type": "tool_use",
                "id": f"tu{offset:.0f}",
                "name": "AskUserQuestion",
                "input": {},
            }
        ]
    else:
        content = [{"type": "text", "text": "working"}]
    event: dict = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "glm-5.3",
            "content": content,
            "stop_reason": stop,
        },
        "uuid": f"a{offset:.0f}",
    }
    if with_ts:
        event["timestamp"] = _iso(offset)
    return _line(**event)


def _tool_result(offset: float, text: str = "done") -> str:
    return _line(
        type="user",
        timestamp=_iso(offset),
        message={
            "role": "user",
            "content": [{"type": "tool_result", "content": text}],
        },
        uuid=f"r{offset:.0f}",
    )


def _queue_op(offset: float) -> str:
    """queue-operation WITH a timestamp — must not extend a turn, but does
    anchor time_first_ts when it is the very first stamped event."""
    return _line(
        type="queue-operation",
        operation="enqueue",
        timestamp=_iso(offset),
        content="<task-notification><task-id>ag-1</task-id>"
        "<status>completed</status></task-notification>",
    )


def _snapshot(offset: float) -> str:
    """file-history-snapshot carries a ts but is neither activity nor boundary."""
    return _line(type="file-history-snapshot", timestamp=_iso(offset), snapshot={})


def _write(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "main.jsonl"
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# basic turn geometry
# ---------------------------------------------------------------------------

def test_single_turn_bounds(tmp_path: Path) -> None:
    """One prompt answered by one assistant → exactly one sub-interval.
    The leading file-history-snapshot anchors time_first_ts even though it
    is neither activity nor a boundary."""
    j = _write(tmp_path, _snapshot(0), _user_prompt(1), _assistant(5))
    r = _scan_main_jsonl(j)

    assert r["time_first_ts"] == ep(0)
    assert r["time_turns"] == [[[ep(1), ep(5)]]]
    assert r["time_open"] is False


def test_several_turns_with_gaps_between_them(tmp_path: Path) -> None:
    """Three prompts separated by idle gaps → three independent turns; the
    gaps between turns belong to nobody here (union_work is applied later)."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(10, "end_turn"),
        _user_prompt(70),
        _assistant(80, "end_turn"),
        _user_prompt(600),
        _assistant(610, "end_turn"),
    )
    r = _scan_main_jsonl(j)

    assert r["time_first_ts"] == ep(0)
    assert r["time_turns"] == [
        [[ep(0), ep(10)]],
        [[ep(70), ep(80)]],
        [[ep(600), ep(610)]],
    ]
    assert r["time_open"] is False


def test_activity_before_first_prompt_is_ignored(tmp_path: Path) -> None:
    """Assistant + tool_result events BEFORE the first real prompt are not
    a turn and never show up in time_turns — but their stamps still anchor
    time_first_ts (any typed event counts)."""
    j = _write(
        tmp_path,
        _assistant(0, "end_turn"),
        _tool_result(50),
        _user_prompt(100),
        _assistant(150, "end_turn"),
    )
    r = _scan_main_jsonl(j)

    assert r["time_first_ts"] == ep(0)
    assert r["time_turns"] == [[[ep(100), ep(150)]]]
    assert r["time_open"] is False


def test_empty_jsonl_yields_time_defaults(tmp_path: Path) -> None:
    """Empty file → no anchor, no turns, not open ('деградация — пустота')."""
    j = tmp_path / "empty.jsonl"
    j.write_text("", encoding="utf-8")

    r = _scan_main_jsonl(j)

    assert r["time_first_ts"] == 0.0
    assert r["time_turns"] == []
    assert r["time_open"] is False


def test_whitespace_only_jsonl_yields_time_defaults(tmp_path: Path) -> None:
    """Blank lines only — same degradation as an empty file."""
    j = tmp_path / "blank.jsonl"
    j.write_text("\n\n   \n\n", encoding="utf-8")

    r = _scan_main_jsonl(j)

    assert r["time_first_ts"] == 0.0
    assert r["time_turns"] == []
    assert r["time_open"] is False


# ---------------------------------------------------------------------------
# open / closed verdicts for the last, still-live turn
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stop_reason", ["tool_use", "pause_turn"])
def test_open_when_last_assistant_stop_extends(
    tmp_path: Path, stop_reason: str
) -> None:
    """A final assistant with stop_reason tool_use / pause_turn leaves the
    turn open (the orchestrator will extend it to now)."""
    j = _write(tmp_path, _user_prompt(0), _assistant(5, stop_reason))
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(5)]]]
    assert r["time_open"] is True


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence"])
def test_closed_on_terminal_stop_reasons(tmp_path: Path, stop_reason: str) -> None:
    """end_turn and stop_sequence both close the session's last turn
    (stop_sequence is the second-most-frequent terminator in real data)."""
    j = _write(tmp_path, _user_prompt(0), _assistant(5, stop_reason))
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(5)]]]
    assert r["time_open"] is False


def test_trailing_tool_results_keep_turn_open(tmp_path: Path) -> None:
    """Chained tool_results after the last assistant mean work is still in
    flight → the live turn stays open, and every tool_result extends the
    turn's end."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(2, "tool_use"),
        _tool_result(3),
        _tool_result(7),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(7)]]]
    assert r["time_open"] is True


def test_assistant_after_tool_results_closes_turn_again(tmp_path: Path) -> None:
    """An end_turn assistant AFTER trailing tool_results supersedes them —
    the turn ends closed."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(2, "tool_use"),
        _tool_result(3),
        _assistant(5, "end_turn"),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(5)]]]
    assert r["time_open"] is False


def test_unanswered_real_prompt_keeps_turn_open(tmp_path: Path) -> None:
    """A prompt with NO assistant response at all is 'last real prompt
    unanswered' → open (the scan may have caught the stream mid-flight)."""
    j = _write(tmp_path, _user_prompt(0))
    r = _scan_main_jsonl(j)

    # A turn without activity records its degenerate marker [[u, u]].
    assert r["time_turns"] == [[[ep(0), ep(0)]]]
    assert r["time_open"] is True


# ---------------------------------------------------------------------------
# interrupts
# ---------------------------------------------------------------------------

def test_interrupt_closes_unanswered_turn(tmp_path: Path) -> None:
    """prompt → interrupt before any assistant response: the interrupt
    terminates the dangling exchange → NOT open. Both the abandoned prompt
    turn and the interrupt-boundary turn degrade to [[u, u]] markers."""
    j = _write(tmp_path, _user_prompt(0), _interrupt(10))
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [
        [[ep(0), ep(0)]],
        [[ep(10), ep(10)]],
    ]
    assert r["time_open"] is False


def test_interrupt_closes_midwork_tool_use_tail(tmp_path: Path) -> None:
    """Even a mid-work state that would otherwise read open (trailing
    tool_results after a tool_use assistant) becomes closed once the user
    interrupted."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(5, "tool_use"),
        _tool_result(8),
        _interrupt(30),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [
        [[ep(0), ep(8)]],
        [[ep(30), ep(30)]],  # the interrupt boundary itself
    ]
    assert r["time_open"] is False


# ---------------------------------------------------------------------------
# non-activity events must not extend a turn
# ---------------------------------------------------------------------------

def test_no_ts_events_and_queue_ops_do_not_extend_turn(tmp_path: Path) -> None:
    """Two kinds of noise after the last assistant event:
    - an assistant event WITHOUT a timestamp (silently skipped), and
    - a queue-operation WITH a timestamp (background-agent notification —
      must not shift the start of waiting).
    The turn keeps ending at the last REAL activity (t=30)."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(30, "end_turn"),
        _assistant(40, "end_turn", with_ts=False),
        _queue_op(90),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(30)]]]
    assert r["time_open"] is False


def test_stamped_non_activity_types_do_not_extend_turn(tmp_path: Path) -> None:
    """A stamped system-ish type (file-history-snapshot) appended at EOF is
    anchored for total but must not extend or reopen anything."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(20, "end_turn"),
        _snapshot(500),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(20)]]]
    assert r["time_open"] is False


# ---------------------------------------------------------------------------
# AskUserQuestion pauses split / trim sub-intervals
# ---------------------------------------------------------------------------

def test_qa_pause_splits_sub_intervals(tmp_path: Path) -> None:
    """QA asked at t=60, answered at t=180: the pause is cut out and the
    turn resumes from the answer moment. Work intervals: [0→60] then
    [180→240]."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(5, "end_turn"),
        _assistant(60, "tool_use", qa=True),
        _tool_result(180, "answered"),
        _assistant(240, "end_turn"),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(60)], [ep(180), ep(240)]]]
    assert r["time_open"] is False


def test_two_sequential_qa_pauses_produce_three_sub_intervals(
    tmp_path: Path,
) -> None:
    """Each ask/answer cycle splits independently; the pieces reassemble in
    order with the pause windows removed."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(60, "tool_use", qa=True),
        _tool_result(180, "a1"),
        _assistant(300, "tool_use", qa=True),
        _tool_result(400, "a2"),
        _assistant(500, "end_turn"),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [
        [[ep(0), ep(60)], [ep(180), ep(300)], [ep(400), ep(500)]]
    ]
    assert r["time_open"] is False


def test_immediate_qa_question_counts_work_up_to_the_question(
    tmp_path: Path,
) -> None:
    """A QA question right after the prompt still yields the elapsed span
    [u → question] as a work piece (thinking/waiting-for-the-ask time)."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(40, "tool_use", qa=True),
        _tool_result(120, "answer"),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(40)], [ep(120), ep(120)]]]
    # Trailing tool_result after the resumed chunk keeps the turn open...
    assert r["time_open"] is True


def test_open_qa_trims_turn_and_forces_closed(tmp_path: Path) -> None:
    """QA asked but NEVER answered: sub-intervals stop AT the question
    (trimmed, nothing leaks past it) and the turn reads closed even though
    the asker was a tool_use assistant."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(5, "end_turn"),
        _assistant(100, "tool_use", qa=True),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(100)]]]
    assert r["time_open"] is False


def test_new_prompt_while_qa_hanging_closes_previous_turn(tmp_path: Path) -> None:
    """The user bails out of an unanswered QA with a fresh prompt: the old
    turn finalizes trimmed-at-question AND closed; the new prompt starts a
    fresh unanswered-prompt turn (open)."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(50, "tool_use", qa=True),
        _user_prompt(400, "forget it, do this instead"),
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [
        [[ep(0), ep(50)]],
        [[ep(400), ep(400)]],
    ]
    assert r["time_open"] is True


def test_assistant_during_open_qa_pause_does_not_extend_turn(
    tmp_path: Path,
) -> None:
    """Inside an unresolved QA pause everything freezes: a stray stamped
    assistant event during the pause neither extends the turn nor flips
    the verdict (the pause window is invisible to segmentation)."""
    j = _write(
        tmp_path,
        _user_prompt(0),
        _assistant(100, "tool_use", qa=True),
        _assistant(150, "end_turn"),  # stray stamp inside the pause window
    )
    r = _scan_main_jsonl(j)

    assert r["time_turns"] == [[[ep(0), ep(100)]]]
    assert r["time_open"] is False
