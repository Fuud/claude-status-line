"""Tests for format_duration / _parse_ts pure functions.

Format rules (from Technical Details):
- format_duration(seconds) -> "HH:MM:SS", hours with no upper bound,
  minutes/seconds zero-padded, negative input clamps to "00:00:00",
  fractional seconds truncate toward zero.
- _parse_ts(value) -> POSIX epoch float | None. Handles the "Z" suffix
  by hand (Python 3.9 fromisoformat rejects it), timezone offsets,
  naive stamps assumed UTC; garbage/None/empty return None.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from status_line import _parse_ts, format_duration


# ---------------------------------------------------------------------------
# format_duration — parametrized coverage of buckets and boundaries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        # base cases from the plan
        (0, "00:00:00"),
        (59.9, "00:00:59"),          # fractional truncates, no carry-up
        (60, "00:01:00"),
        (3599, "00:59:59"),
        (3600, "01:00:00"),
        (86_400, "24:00:00"),        # 24h — hours stay unpadded past 99 below
        (372_310, "103:25:10"),      # 100h+ case — hours field grows freely
        # hour field below 10 keeps two digits
        (3 * 3600 + 45 * 60 + 12, "03:45:12"),
    ],
)
def test_format_duration(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    "seconds", [-1, -0.5, -100, -3600, -(2**31)]
)
def test_format_duration_negative_clamped_to_zero(seconds: float) -> None:
    """Negative durations clamp to "00:00:00"; wait computation already
    clamps >= 0 upstream, so this is a defensive guard mirroring
    format_tokens."""
    assert format_duration(seconds) == "00:00:00"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.999, "00:00:00"),         # < 1s truncated away entirely
        (1.5, "00:00:01"),           # truncate, never round up
        (62.7, "00:01:02"),          # truncation applies to whole number
    ],
)
def test_format_duration_fractional_seconds_truncate(
    seconds: float, expected: str
) -> None:
    assert format_duration(seconds) == expected


# ---------------------------------------------------------------------------
# _parse_ts — ISO 8601 variants
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int, minute: int, second: int,
         microsecond: int = 0) -> float:
    """Reference epoch for a fixed UTC wall-clock time (deterministic)."""
    return datetime(year, month, day, hour, minute, second, microsecond,
                    tzinfo=timezone.utc).timestamp()


def test_parse_ts_z_suffix() -> None:
    ts = _parse_ts("2026-08-27T12:30:05Z")
    assert ts == _utc(2026, 8, 27, 12, 30, 5)


def test_parse_ts_explicit_utc_offset() -> None:
    ts = _parse_ts("2026-08-27T12:30:05+00:00")
    assert ts == _utc(2026, 8, 27, 12, 30, 5)


def test_parse_ts_timezone_offset_normalized() -> None:
    # +02:00 offset converts back to the same instant as 12:30 UTC.
    ts = _parse_ts("2026-08-27T14:30:05+02:00")
    assert ts == _utc(2026, 8, 27, 12, 30, 5)

    ts_neg = _parse_ts("2026-08-27T09:30:05-03:00")
    assert ts_neg == _utc(2026, 8, 27, 12, 30, 5)


def test_parse_ts_naive_assumed_utc() -> None:
    # No zone designator at all → assumed UTC, not local time (the status
    # line runs on machines with arbitrary local zones).
    ts = _parse_ts("2026-08-27T12:30:05")
    assert ts == _utc(2026, 8, 27, 12, 30, 5)


def test_parse_ts_milliseconds_preserved() -> None:
    # Session jsonl stamps are ISO with millisecond precision.
    ts = _parse_ts("2026-08-27T12:30:05.123Z")
    assert ts == _utc(2026, 8, 27, 12, 30, 5, microsecond=123_000)


@pytest.mark.parametrize(
    "value",
    ["", "not-a-timestamp", "2026-13-45T99:99:99Z", "27/08/2026"],
)
def test_parse_ts_garbage_returns_none(value: str) -> None:
    assert _parse_ts(value) is None


@pytest.mark.parametrize("value", [None, 17, [], {}])
def test_parse_ts_non_string_returns_none(value: object) -> None:
    assert _parse_ts(value) is None
