"""Tests for render_table — the generic column renderer (Task 4).

render_table(columns, rows) renders the label row (from the column dicts'
labels) followed by one line per row, WITHOUT the "| " table-row prefix
(prefixing is render_output's concern).

Column width = max(floor, len(label), longest cell in that column across
all rows). "align" picks ljust ("left") or rjust ("right"). The optional
"gap" key (default: a single space) is the separator glued after the
column. label/align/floor are REQUIRED and every row must carry exactly
one cell per column — a mis-shaped row/column raises instead of silently
rendering blank cells (row-shape bugs surface at the call site).
Rendered lines never carry trailing whitespace — an empty cell in the
LAST column leaves no padding spaces at the end of the line.
"""
from __future__ import annotations

import pytest

from status_line import render_table


# ---------------------------------------------------------------------------
# widths: floor / label / content each get to win
# ---------------------------------------------------------------------------

def test_width_floor_beats_label_and_content() -> None:
    """floor=7 wins over a 2-char label and a 3-char cell."""
    columns = [{"label": "in", "align": "right", "floor": 7}]
    rows = [["500"]]
    assert render_table(columns, rows) == ["     in", "    500"]


def test_width_label_beats_content() -> None:
    """A 6-char label beats a 2-char cell; floor 0 stays out of the way."""
    columns = [{"label": "cached", "align": "right", "floor": 0}]
    rows = [["42"]]
    assert render_table(columns, rows) == ["cached", "    42"]


def test_width_content_beats_floor_and_label() -> None:
    """An 8-char cell beats floor 2 and the label."""
    columns = [{"label": "in", "align": "right", "floor": 2}]
    rows = [["12345.7M"]]
    assert render_table(columns, rows) == ["      in", "12345.7M"]


def test_width_computed_per_column() -> None:
    """Each column's width is independent of the others."""
    columns = [
        {"label": "in", "align": "right", "floor": 0},
        {"label": "out", "align": "right", "floor": 0},
    ]
    rows = [["7", "12345.7M"]]
    # w_in = max(0, 2, 1) = 2; w_out = max(0, 3, 8) = 8
    assert render_table(columns, rows) == ["in      out", " 7 12345.7M"]


# ---------------------------------------------------------------------------
# alignment
# ---------------------------------------------------------------------------

def test_left_alignment_pads_on_the_right() -> None:
    """left-aligned cells pad with trailing spaces so the NEXT column
    starts at the same x-position for every row."""
    columns = [
        {"label": "model", "align": "left", "floor": 0},
        {"label": "in", "align": "right", "floor": 3},
    ]
    rows = [
        ["glm", "7"],
        ["MiniMax-M3", "42"],
    ]
    # w_model = max(0, 5, 10) = 10; w_in = max(3, 2, 2) = 3
    assert render_table(columns, rows) == [
        "model       in",
        "glm          7",
        "MiniMax-M3  42",
    ]


def test_right_alignment_pads_on_the_left() -> None:
    """right-aligned cells pad with leading spaces; the previous column
    ends at the same x-position for every row."""
    columns = [
        {"label": "name", "align": "left", "floor": 0},
        {"label": "in", "align": "right", "floor": 0},
    ]
    rows = [
        ["a", "7"],
        ["bb", "12345.7M"],
    ]
    # w_name = max(0, 4, 2) = 4; w_in = max(0, 2, 8) = 8
    assert render_table(columns, rows) == [
        "name       in",
        "a           7",
        "bb   12345.7M",
    ]


# ---------------------------------------------------------------------------
# empty cells
# ---------------------------------------------------------------------------

def test_empty_cell_in_left_column_renders_blank() -> None:
    """An empty cell in a non-last column renders as the column's padding
    (blank gap) — the continuation-row shape used by multi-row groups."""
    columns = [
        {"label": "model", "align": "left", "floor": 0},
        {"label": "in", "align": "right", "floor": 0},
    ]
    rows = [
        ["glm-5.3", "12"],
        ["", "7"],
    ]
    # w_model = max(0, 5, 7) = 7; w_in = max(0, 2, 2) = 2
    assert render_table(columns, rows) == [
        "model   in",
        "glm-5.3 12",
        "         7",  # "" ljust 7 + gap + "7" rjust 2 → 9 spaces
    ]


def test_empty_last_cell_leaves_no_trailing_whitespace() -> None:
    """An empty cell in the LAST column must not leave padding spaces at
    the end of the line (rows are right-stripped)."""
    columns = [
        {"label": "model", "align": "left", "floor": 0},
        {"label": "cost", "align": "right", "floor": 0},
    ]
    rows = [
        ["glm-5.3", "$8.1"],
        ["glm-5.3", ""],
    ]
    # w_model = max(0, 5, 7) = 7; w_cost = max(0, 4, 0) = 4
    assert render_table(columns, rows) == [
        "model   cost",
        "glm-5.3 $8.1",
        "glm-5.3",  # stripped: no trailing 5 spaces of empty cost cell
    ]


def test_ragged_row_raises_instead_of_rendering_blanks() -> None:
    """A row with fewer cells than there are columns raises IndexError —
    render_table intentionally has no ragged-row tolerance: silently
    rendering blank cells would mask row-shape bugs at the call site."""
    columns = [
        {"label": "model", "align": "left", "floor": 0},
        {"label": "in", "align": "right", "floor": 0},
    ]
    rows = [["glm-5.3"]]  # second cell missing entirely
    with pytest.raises(IndexError):
        render_table(columns, rows)


def test_missing_required_column_key_raises() -> None:
    """label/align/floor are required column keys — an omitted key raises
    KeyError rather than silently defaulting."""
    with pytest.raises(KeyError):
        render_table([{"label": "in", "align": "right"}], [["1"]])  # no floor
    with pytest.raises(KeyError):
        render_table([{"label": "in", "floor": 0}], [["1"]])  # no align
    with pytest.raises(KeyError):
        render_table([{"align": "right", "floor": 0}], [["1"]])  # no label


def test_no_rows_renders_label_row_only() -> None:
    """An empty rows list renders just the label row (widths fall back to
    max(floor, len(label), 0))."""
    columns = [
        {"label": "in", "align": "right", "floor": 7},
    ]
    assert render_table(columns, []) == ["     in"]


# ---------------------------------------------------------------------------
# gaps and the label row
# ---------------------------------------------------------------------------

def test_default_gap_is_single_space() -> None:
    columns = [
        {"label": "in", "align": "right", "floor": 0},
        {"label": "out", "align": "right", "floor": 0},
    ]
    rows = [["1", "2"]]
    # w_in = max(0, 2, 1) = 2; w_out = max(0, 3, 1) = 3
    assert render_table(columns, rows) == ["in out", " 1   2"]


def test_custom_gap_key() -> None:
    """The optional "gap" key widens the separator after that column —
    render_output uses it to reproduce the 2-space description gap."""
    columns = [
        {"label": "", "align": "left", "floor": 2, "gap": "  "},
        {"label": "in", "align": "right", "floor": 0},
    ]
    rows = [["x", "7"]]
    # w0 = max(2, 0, 1) = 2; w1 = max(0, 2, 1) = 2
    assert render_table(columns, rows) == ["    in", "x    7"]


def test_label_row_is_first_line() -> None:
    """The first returned line is the label row built from the column
    dicts — including for empty labels (leading blank column)."""
    columns = [
        {"label": "", "align": "left", "floor": 4},
        {"label": "in", "align": "right", "floor": 0},
    ]
    lines = render_table(columns, [["", "5"]])
    assert len(lines) == 2
    # label row: "" ljust 4 + gap + "in" rjust 2 → blank then the label
    assert lines[0] == "     in"
    assert lines[1] == "      5"
