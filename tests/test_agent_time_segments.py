"""Direct unit tests for _agent_time_segments (pure function of an agent
snapshot dict + a frozen `now`) — the pause/cut/extension geometry that
feeds both the session union and the agent's own work/wait/total triple.

Covered geometry (plan 20260827-status-line-time-columns):
- closed pauses cut the lifetime into work sub-intervals, clipped into the
  live window; wait counts only the clipped overlap;
- pause overlapping either lifetime edge is clipped, a pause entirely
  outside the window is ignored;
- run-status agents extend life_end to now (max-guard against a stamp
  already ahead of a skewed clock);
- an OPEN question trims life_end at the question moment (trim wins over
  the run extension) and keeps growing wait as now advances — WITHOUT
  erasing the work performed before the question;
- hand-corrupted-cache guards: inverted pairs (p_end <= p_start), junk
  pair shapes and null/0/inverted lifetime stamps degrade instead of
  poisoning the arithmetic.
"""
from __future__ import annotations

from status_line import _agent_time_segments


NOW = 10_000.0


def _agent(**overrides: object) -> dict:
    """Baseline agent: lifetime [100, 1000], no pauses, status ok."""
    base: dict = {
        "agentId": "agent-x",
        "ts_first": 100.0,
        "ts_last": 1000.0,
        "qa_pauses": [],
        "qa_open_ts": 0.0,
        "status": "ok",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# closed-pause geometry
# ---------------------------------------------------------------------------

def test_single_closed_pause_cuts_lifetime() -> None:
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[500.0, 600.0]]), NOW
    )
    assert ivs == [[100.0, 500.0], [600.0, 1000.0]]
    assert work == 800.0
    assert wait == 100.0
    assert total == 900.0
    assert work + wait == total


def test_multiple_unsorted_pairs_all_cut() -> None:
    # pairs arrive out of order (hand-edited cache) — the helper sorts them
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[700.0, 800.0], [300.0, 350.0]]), NOW
    )
    assert ivs == [
        [100.0, 300.0], [350.0, 700.0], [800.0, 1000.0],
    ]
    assert wait == 150.0
    assert work == 750.0
    assert work + wait == total == 900.0


def test_adjacent_pause_touching_lifetime_start_clipped() -> None:
    # pause starts BEFORE ts_first and ends inside — clipped to [100, 400]
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[20.0, 400.0]]), NOW
    )
    assert ivs == [[400.0, 1000.0]]
    assert wait == 300.0
    assert work == 600.0


def test_pause_overhanging_lifetime_end_clipped() -> None:
    # pause starts inside and ends after ts_last — clipped to [800, 1000]
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[800.0, 5000.0]]), NOW
    )
    assert ivs == [[100.0, 800.0]]
    assert wait == 200.0
    assert work == 700.0


def test_pauses_entirely_outside_window_ignored() -> None:
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[10.0, 50.0], [2000.0, 3000.0]]), NOW
    )
    assert ivs == [[100.0, 1000.0]]
    assert wait == 0.0
    assert work == total == 900.0


def test_pauses_covering_whole_lifetime_leave_zero_work() -> None:
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[100.0, 1000.0]]), NOW
    )
    assert ivs == []
    assert work == 0.0
    assert wait == 900.0 == total


# ---------------------------------------------------------------------------
# run-status extension / open-question trim
# ---------------------------------------------------------------------------

def test_run_agent_without_open_qa_extends_to_now() -> None:
    # ts_last=1000 far in the past, now=10000 → lifetime grows to now
    ivs, work, wait, total = _agent_time_segments(_agent(status="run"), NOW)
    assert ivs == [[100.0, NOW]]
    assert work == total == NOW - 100.0
    assert wait == 0.0


def test_run_extension_never_shrinks_a_future_stamp() -> None:
    # skewed clock: now is BEHIND ts_last — max() keeps the stamp
    ivs, work, wait, total = _agent_time_segments(_agent(status="run"), 500.0)
    assert ivs == [[100.0, 1000.0]]
    assert work == total == 900.0


def test_non_run_status_does_not_extend() -> None:
    ivs, work, wait, total = _agent_time_segments(_agent(status="ok"), NOW)
    assert ivs == [[100.0, 1000.0]]
    assert total == 900.0


def test_open_question_trims_at_question_moment() -> None:
    # question at 600 while ts_last=1000: trim wins, even for run status
    for status in ("run", "ok"):
        ivs, work, wait, total = _agent_time_segments(
            _agent(qa_open_ts=600.0, status=status), NOW
        )
        assert ivs == [[100.0, 600.0]], status
        assert total == 500.0, status


def test_open_question_wait_grows_with_now_but_keeps_performed_work() -> None:
    """THE hanging-question regression: work performed before the question
    is never erased by the growing open gap; the gap is a plain wait
    addend (wait > total is the honest picture while the question hangs)."""
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_open_ts=600.0, status="run"), NOW
    )
    assert work == 500.0            # [100 → 600] survived, NOT 00:00
    assert total == 500.0
    assert wait == NOW - 600.0      # 9400 — the open gap alone
    assert wait > total             # allowed on agent rows
    # as now advances only wait moves
    _, work2, wait2, total2 = _agent_time_segments(
        _agent(qa_open_ts=600.0, status="run"), NOW + 500.0
    )
    assert (work2, total2) == (work, total)
    assert wait2 == wait + 500.0


def test_open_question_with_closed_pause_before_it() -> None:
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[200.0, 300.0]], qa_open_ts=600.0, status="run"),
        NOW,
    )
    assert ivs == [[100.0, 200.0], [300.0, 600.0]]
    assert total == 500.0
    assert work == 400.0            # 500 minus the 100s closed pause
    assert wait == 100.0 + (NOW - 600.0)


def test_open_question_before_ts_first_clamped_to_lifetime() -> None:
    # junk qa_open_ts < ts_first clamps to ts_first → empty lifetime
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_open_ts=50.0), NOW
    )
    assert ivs == []
    assert total == 0.0
    assert wait == NOW - 50.0       # the gap still grows


def test_open_question_after_ts_last_clamped_to_ts_last() -> None:
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_open_ts=5000.0), NOW
    )
    assert ivs == [[100.0, 1000.0]]
    assert total == work == 900.0
    assert wait == NOW - 5000.0


# ---------------------------------------------------------------------------
# hand-corrupted-cache guards
# ---------------------------------------------------------------------------

def test_inverted_pause_pairs_skipped() -> None:
    """p_end <= p_start must be dropped, not folded in: a negative wait
    contribution would drag the cursor backwards, emit overlapping work
    sub-intervals and render work > total."""
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[1000.0, 500.0], [700.0, 750.0]]), NOW
    )
    assert ivs == [[100.0, 700.0], [750.0, 1000.0]]
    assert wait == 50.0             # only the valid pair counted
    assert work == 850.0
    assert work <= total


def test_degenerate_pause_pairs_skipped() -> None:
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[500.0, 500.0]]), NOW
    )
    assert ivs == [[100.0, 1000.0]]
    assert wait == 0.0


def test_junk_pause_shapes_skipped() -> None:
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[
            "not-a-pair",
            [500.0],                      # wrong arity
            [None, 600.0],                # null edge
            ["garbage", "junk"],          # non-numeric edges
            [500.0, 600.0],               # the one valid pair
        ]),
        NOW,
    )
    assert ivs == [[100.0, 500.0], [600.0, 1000.0]]
    assert wait == 100.0


def test_null_or_zero_or_inverted_lifetime_degrades_to_none() -> None:
    assert _agent_time_segments(_agent(ts_first=None), NOW) is None
    assert _agent_time_segments(_agent(ts_last=None), NOW) is None
    # time fields entirely absent (pre-upgrade cache entry) → None
    assert _agent_time_segments({"agentId": "a", "status": "ok"}, NOW) is None
    assert _agent_time_segments(_agent(ts_first=0.0), NOW) is None
    assert _agent_time_segments(_agent(ts_last=0.0), NOW) is None
    # inverted lifetime (ts_last < ts_first) — corrupt, not negative
    assert _agent_time_segments(_agent(ts_last=50.0), NOW) is None


def test_null_pauses_field_treated_as_empty() -> None:
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=None), NOW
    )
    assert ivs == [[100.0, 1000.0]]
    assert wait == 0.0


def test_non_finite_stamps_degrade_to_none() -> None:
    """json.loads parses bare NaN/Infinity; the coercion must reject them
    (a nan is truthy — it would slip past `or 0.0` and poison format_duration
    into a ValueError that degrades the WHOLE status line)."""
    nan = float("nan")
    inf = float("inf")
    assert _agent_time_segments(_agent(ts_first=nan), NOW) is None
    assert _agent_time_segments(_agent(ts_last=inf), NOW) is None
    assert _agent_time_segments(_agent(qa_open_ts=nan), NOW) is not None
    ivs, work, wait, total = _agent_time_segments(
        _agent(qa_pauses=[[nan, 600.0]]), NOW
    )
    assert ivs == [[100.0, 1000.0]]  # nan-edged pair skipped
    assert wait == 0.0
