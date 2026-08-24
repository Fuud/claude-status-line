"""Tests for format_tokens pure function.

Format rules (from Technical Details):
- n < 1000:           raw integer as string
- 1000 <= n < 1_000_000:  "Nk" (no decimals)
- n >= 1_000_000:      "N.NM" (1 decimal, e.g. "1.2M")

[decision] format_tokens(999500) → "1000k" (we round DOWN to nearest k, so 999500
displays as "1000k" because 999500 / 1000 = 999.5 → integer division rounds to 999,
wait no — 999500 >= 1000 and < 1_000_000 so it's "Nk" branch, 999500 // 1000 = 999,
999 * 1000 = 999000 → but 999500 != 999000, so plain integer division loses precision.
The current implementation uses n // 1000, so 999500 // 1000 = 999 → "999k".
That contradicts the test expectation "1000k". Decision: use math.floor rounding
in a way that gives "1000k" for 999500 — i.e. round to nearest, not truncate.
We will pick "1000k" per the explicit test requirement (round 999500 → 1000k).
"""
from __future__ import annotations

import pytest

from status_line import format_tokens


# ---------------------------------------------------------------------------
# Core format rules — parametrized coverage of buckets and boundaries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0"),
        (1, "1"),
        (850, "850"),
        (999, "999"),
        # k boundary
        (1000, "1k"),
        (1500, "2k"),     # round-half-up: 1.5 → 2
        (78000, "78k"),
        (999500, "1000k"),  # [decision] round-half-up to nearest k
        # M boundary
        (1_000_000, "1.0M"),
        (1_234_567, "1.2M"),
        (12_345_678, "12.3M"),
        # rounding up M (9.96 → 10.0, not 10)
        (9_960_000, "10.0M"),
        # exact threshold
        (999_999, "1000k"),
        (10_000_000, "10.0M"),
    ],
)
def test_format_tokens(n: int, expected: str) -> None:
    assert format_tokens(n) == expected


# ---------------------------------------------------------------------------
# Defensive: negative inputs clamp to 0
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [-1, -100, -999_999, -1_000_000, -(2**31)])
def test_format_tokens_negative_clamped_to_zero(n: int) -> None:
    """Negative inputs are clamped to 0; the status line never shows negative
    values. This is a defensive guard, not a public contract — see the
    module docstring on `format_tokens`."""
    assert format_tokens(n) == "0"


# ---------------------------------------------------------------------------
# Boundary: 999 → "999" (just under k threshold), 1000 → "1k"
# ---------------------------------------------------------------------------

def test_format_tokens_just_below_k_threshold() -> None:
    assert format_tokens(999) == "999"
    assert format_tokens(999_499) == "999k"  # 999499 / 1000 = 999.499 → 999


def test_format_tokens_just_above_k_threshold() -> None:
    assert format_tokens(1000) == "1k"
    assert format_tokens(1001) == "1k"


def test_format_tokens_just_below_m_threshold() -> None:
    # 999_999 / 1_000_000 = 0.999999 → rounds to 1.0 → "1.0M"
    assert format_tokens(999_999) == "1000k"
    # 999_500 / 1_000 = 999.5 → "1000k" (still in k branch)