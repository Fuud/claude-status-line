"""Tests for union_work — pure function summing a union of intervals.

Rules (from Technical Details):
- input is a list of [start, end] epoch pairs;
- overlapping AND adjacent intervals merge ([0,10] + [10,20] → 20);
- degenerate intervals (end <= start) are silently dropped;
- returned value is the total covered time (union, never double-counted);
- empty / all-degenerate input → 0.0.
"""
from __future__ import annotations

import pytest

from status_line import union_work


def test_union_work_empty_list_is_zero() -> None:
    assert union_work([]) == 0


def test_union_work_single_interval() -> None:
    assert union_work([[100, 250]]) == 150


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        # overlap: [0,10] and [5,15] cover [0,15]
        ([[0, 10], [5, 15]], 15),
        # partial overlap on the right edge
        ([[5, 15], [10, 12]], 10),
    ],
)
def test_union_work_overlapping_merges(
    intervals: list[list[float]], expected: float
) -> None:
    assert union_work(intervals) == expected


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        # nested fully inside — counted once
        ([[0, 100], [10, 20]], 100),
        # nested in reverse order — same result
        ([[10, 20], [0, 100]], 100),
        # equal spans duplicated
        ([[30, 40], [30, 40]], 10),
    ],
)
def test_union_work_nested_counted_once(
    intervals: list[list[float]], expected: float
) -> None:
    assert union_work(intervals) == expected


def test_union_work_adjacent_intervals_merge() -> None:
    # Touching endpoints bridge into one span (the QA-pause split leaves
    # turns as adjacent sub-intervals; a zero-width seam must not leak).
    assert union_work([[0, 10], [10, 20]]) == 20


def test_union_work_unsorted_input() -> None:
    # sorted → [0,10]=10, [25,35]=10, [50,60]+[55,70]=[50,70]=20
    assert union_work([[50, 60], [0, 10], [25, 35], [55, 70]]) == 40


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        # zero-length (e == s) dropped
        ([[0, 10], [5, 5]], 10),
        # inverted (e < s) dropped
        ([[0, 10], [8, 4]], 10),
        # all degenerate → 0
        ([[7, 7], [9, 1]], 0),
    ],
)
def test_union_work_degenerate_intervals_dropped(
    intervals: list[list[float]], expected: float
) -> None:
    assert union_work(intervals) == expected


def test_union_work_holes_sum_from_pieces() -> None:
    # A turn split by a QA pause arrives as disjoint sub-intervals;
    # the gap between them must not count toward work.
    assert union_work([[0, 10], [20, 30], [40, 41]]) == 21


def test_union_work_fractional_seconds() -> None:
    # Epoch values are floats with ms precision.
    total = union_work([[0.5, 1.25], [1.0, 2.0]])
    assert abs(total - 1.5) < 1e-9


def test_union_work_does_not_mutate_input() -> None:
    # Callers pass scan-result lists that outlive the call (cache fields,
    # orchestrator re-use) — merging must sort a copy, not reorder in place.
    intervals = [[50, 60], [0, 10]]
    union_work(intervals)
    assert intervals == [[50, 60], [0, 10]]
