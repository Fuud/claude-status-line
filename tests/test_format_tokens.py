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


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0"),
        (850, "850"),
        (999, "999"),
        (1000, "1k"),
        (78000, "78k"),
        (999500, "1000k"),  # [decision] round-half-up to nearest k
        (1234567, "1.2M"),
    ],
)
def test_format_tokens(n: int, expected: str) -> None:
    assert format_tokens(n) == expected