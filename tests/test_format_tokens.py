"""Tests for format_tokens pure function.

Format rules (from Technical Details):
- n < 1000:           raw integer as string
- 1000 <= n < 1_000_000:  "Nk" (no decimals)
- n >= 1_000_000:      "N.NM" (1 decimal, e.g. "1.2M")

[decision] format_tokens uses Python's round() (banker's rounding) for the
k and M buckets. 999500 / 1000 = 999.5 → rounds to 1000 (not 999) because
banker's rounding still rounds .5 to even — and 1000 is the next even
integer — yielding "1000K" per the explicit test requirement.
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
        (1000, "1K"),
        (1500, "2K"),     # round-half-up: 1.5 → 2
        (78000, "78K"),
        (999500, "1000K"),  # [decision] round-half-up to nearest k
        # M boundary
        (1_000_000, "1.0M"),
        (1_234_567, "1.2M"),
        (12_345_678, "12.3M"),
        # rounding up M (9.96 → 10.0, not 10)
        (9_960_000, "10.0M"),
        # exact threshold
        (999_999, "1000K"),
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
# Boundary: 999 → "999" (just under k threshold), 1000 → "1K"
# ---------------------------------------------------------------------------

def test_format_tokens_just_below_k_threshold() -> None:
    assert format_tokens(999) == "999"
    assert format_tokens(999_499) == "999K"  # 999499 / 1000 = 999.499 → 999


def test_format_tokens_just_above_k_threshold() -> None:
    assert format_tokens(1000) == "1K"
    assert format_tokens(1001) == "1K"


def test_format_tokens_just_below_m_threshold() -> None:
    # 999_999 is still in the k branch (< 1_000_000); 999_999 / 1000 = 999.999
    # → rounds to 1000 → "1000K" (not "1.0M").
    assert format_tokens(999_999) == "1000K"