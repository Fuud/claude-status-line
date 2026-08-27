"""Tests for render_output — assemble the multi-line status line string.

render_output(header, start_in, start_out, start_cached, main_models,
agents, prices=None, host="") returns a string built as:
    header
    | <table header — labels "model" / "in" / "out" / "cached" / "cost">
    | start: <in> <out> <cached>
    | sum: [<model>] <in> <out> <cached> [<cost>]  # only if len(agents) > 0
    | main: [<model>] <in> <out> <cached> [<cost>]
    | for each agent (in input order):
        "[<status>]  <description>  [<model>]  <in> <out> <cached> [<cost>]"

main_models is the per-model breakdown dict {model_id: {"in","out","cached"}}
(the flat main_in/main_out/main_cached triple is gone — the main row's
totals are the sum of its per-model records).

prices=None (no prices.json) → NO model and NO cost columns: one row per
group, group totals — the pre-model-columns ROW shape. [deviation] Since
the time columns (plan 20260827-status-line-time-columns) BOTH layouts
additionally close every row with the always-visible work/wait/total
block: empty cells without time data, HH:MM:SS values when the caller
supplies main_time / agent time_* fields. prices present → the model
column sits between the description and `in` (left-aligned), the cost
column after `cached` (right-aligned), and each group (sum/main/agent)
expands to one row PER MODEL in first-appearance order; zero-token
per-model records (including <synthetic>) are skipped; a group left with
no rows renders ONE zero row with an EMPTY model cell (agents are never
skipped).

The start row (first-message breakdown) is always rendered, is NOT part
of the sum row, and never carries model/cost cells.

Every table row (all lines except the session header) starts with the
"| " prefix (_TABLE_ROW_PREFIX) so Claude Code's leading-whitespace strip
cannot left-shift the all-spaces token-header row.

Every numeric cell is formatted through format_tokens() (so 1000 → "1K")
and right-aligned to a per-column width (max of label length, the widest
formatted cell value, and _TOKEN_COLUMN_WIDTH=7). Each column's width is
computed independently.

Status icons: "[ok]", "[run]", "[err]", "[stop]", "[kill]". Description
>40 chars is truncated with U+2026.

Line layout for a single-agent scenario without prices:
    [0] header
    [1] table header (in / out / cached)
    [2] start
    [3] sum
    [4] main
    [5] agent
"""
from __future__ import annotations

import re
from pathlib import Path

from status_line import (
    _DESC_TOKEN_GAP,
    _ICON_COL_WIDTH,
    _STATUS_GAP,
    _TABLE_ROW_PREFIX,
    _TOKEN_COLUMN_WIDTH,
    format_tokens,
    render_output,
)


def _col_width(values: list, label: str) -> int:
    """The token-column width formula (former production helper _col_width,
    inlined here when it lost its last production caller): max of the
    _TOKEN_COLUMN_WIDTH floor, the label, and the widest formatted cell."""
    longest_value = max((len(format_tokens(v)) for v in values), default=0)
    return max(_TOKEN_COLUMN_WIDTH, len(label), longest_value)


def _main(in_v: int, out_v: int, cached_v: int) -> dict:
    """Single-record main_models dict reproducing the old flat triple —
    the conversion target for the pre-model-columns tests below."""
    return {"glm-5.3": {"in": in_v, "out": out_v, "cached": cached_v}}


# ---------------------------------------------------------------------------
# single ok agent
# ---------------------------------------------------------------------------

def test_single_ok_agent() -> None:
    """1 agent [ok] with breakdown → 6 lines: header, table header, start,
    sum, main, agent line. Each numeric cell formatted via format_tokens."""
    header = "Session: abc | Branch: master | Model: X | User: u"
    agents = [
        {
            "status": "ok",
            "tokens_in": 300,
            "tokens_out": 400,
            "tokens_cached": 100,
            "description": "Task 1: foo",
        },
    ]

    # start=(100, 30, 200) — mirrors the main_normal fixture's first event.
    out = render_output(header, 100, 30, 200, _main(1000, 500, 200), agents)
    lines = out.split("\n")

    # header + table header + start + sum + main + agent = 6
    assert len(lines) == 6
    assert lines[0] == header
    # table header line contains the three labels
    assert "in" in lines[1]
    assert "out" in lines[1]
    assert "cached" in lines[1]
    # start line: 100→"100", 30→"30", 200→"200"
    assert lines[2].startswith(_TABLE_ROW_PREFIX + "start:")
    assert "100" in lines[2]
    assert "30" in lines[2]
    assert "200" in lines[2]
    # sum line: in=1300→"1K", out=900→"900", cached=300→"300"
    assert lines[3].startswith(_TABLE_ROW_PREFIX + "sum:")
    assert "1K" in lines[3]
    assert "900" in lines[3]
    assert "300" in lines[3]
    # main line: 1000→"1K", 500→"500", 200→"200"
    assert lines[4].startswith(_TABLE_ROW_PREFIX + "main:")
    assert "1K" in lines[4]
    assert "500" in lines[4]
    assert "200" in lines[4]
    # agent line: starts with [ok], contains description and three numbers
    assert lines[5].startswith(_TABLE_ROW_PREFIX + "[ok]")
    assert "Task 1: foo" in lines[5]
    assert "300" in lines[5]
    assert "400" in lines[5]
    assert "100" in lines[5]


# ---------------------------------------------------------------------------
# zero agents — no sum line
# ---------------------------------------------------------------------------

def test_zero_agents_no_sum_line() -> None:
    """0 agents → header + table header + start + main only (no sum line)."""
    header = "Session: abc"
    out = render_output(header, 7, 8, 9, _main(0, 42, 0), [])
    lines = out.split("\n")

    # header + table header + start + main = 4
    assert len(lines) == 4
    assert lines[0] == header
    # table header line follows
    assert "in" in lines[1] and "out" in lines[1] and "cached" in lines[1]
    # start line precedes main
    assert lines[2].startswith(_TABLE_ROW_PREFIX + "start:")
    assert "7" in lines[2] and "8" in lines[2] and "9" in lines[2]
    # main line follows
    assert lines[3].startswith(_TABLE_ROW_PREFIX + "main:")
    assert "42" in lines[3]
    # no "sum:" line at all
    assert "sum:" not in out


# ---------------------------------------------------------------------------
# 38 agents → 43 lines (header + table header + start + sum + main + 38 agents)
# ---------------------------------------------------------------------------

def test_38_agents_produce_43_lines() -> None:
    """38 agents → 43 lines: 1 header + 1 table header + 1 start + 1 sum +
    1 main + 38 agent lines."""
    header = "Session: big | Branch: m | Model: X | User: u"
    agents = [
        {
            "status": "ok",
            "tokens_in": (i + 1) * 10,
            "tokens_out": (i + 1) * 5,
            "tokens_cached": (i + 1) * 3,
            "description": f"Agent {i}",
        }
        for i in range(38)
    ]

    out = render_output(header, 100, 30, 200, _main(5000, 2000, 1000), agents)
    lines = out.split("\n")

    assert len(lines) == 43
    assert lines[0] == header
    # table header is line 1
    assert "in" in lines[1] and "out" in lines[1] and "cached" in lines[1]
    assert lines[2].startswith(_TABLE_ROW_PREFIX + "start:")
    assert lines[3].startswith(_TABLE_ROW_PREFIX + "sum:")
    assert lines[4].startswith(_TABLE_ROW_PREFIX + "main:")
    # remaining 38 lines all start with the table prefix + a status tag
    for line in lines[5:]:
        assert line.startswith(_TABLE_ROW_PREFIX + "[")


# ---------------------------------------------------------------------------
# token alignment — right-aligned to a per-column fixed width
# ---------------------------------------------------------------------------

def test_token_alignment_right_aligned() -> None:
    """Each numeric column is right-aligned to its own fixed width, and
    integer values are formatted via format_tokens BEFORE padding to
    width (1234567 → "1.2M", 50000 → "50K", NOT the raw digits). This
    exercises the format_tokens-before-:>W rule with a wide range of
    magnitudes."""
    header = "Session: x"
    agents = [
        # tokens_in: 10, 50000, 1234567 — widest is "1.2M" (4 chars)
        {"status": "ok", "tokens_in": 10,      "tokens_out": 0, "tokens_cached": 0, "description": "a"},
        {"status": "ok", "tokens_in": 50000,   "tokens_out": 0, "tokens_cached": 0, "description": "b"},
        {"status": "ok", "tokens_in": 1234567, "tokens_out": 0, "tokens_cached": 0, "description": "c"},
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    lines = out.split("\n")
    agent_lines = lines[5:]  # header + table header + start + sum + main + agents

    formatted_in = ["10", "50K", "1.2M"]
    # The "in" column width is W1 = max(_TOKEN_COLUMN_WIDTH=7, 2, 4) = 7.
    # Each value is right-aligned to width 7 → ends at column position
    # len(prefix) + W1. The prefix is "[ok]  <desc>  " (variable desc width).
    # Within each row, the END of the "in" cell is the same column index.
    end_positions = []
    for line, expected in zip(agent_lines, formatted_in):
        assert expected in line, (
            f"expected formatted {expected!r} in line {line!r} "
            f"(format_tokens must be applied before right-alignment)"
        )
        end = line.rfind(expected) + len(expected)
        end_positions.append(end)

    # all three `in` cells end at the same column → right-aligned
    assert len(set(end_positions)) == 1, (
        f"tokens_in not right-aligned: ends={end_positions} from "
        f"lines {agent_lines}"
    )


# ---------------------------------------------------------------------------
# three columns right-aligned independently
# ---------------------------------------------------------------------------

def test_three_columns_right_aligned() -> None:
    """Three agents where each column has a different widest value. Each
    column's width is computed from its own data + label, independently of
    the other columns. Same descriptions so the per-row start position is
    identical across the three rows — that lets us check END positions
    directly."""
    header = "Session: x"
    # tokens_in widest=2000 → "2K" (2 chars)
    # tokens_out widest=1234 → "1K" (2 chars)
    # tokens_cached widest=1234567 → "1.2M" (4 chars)
    # widths: max(7, 2, len("1.2M")=4) = 7 for cached; 7 for in; 7 for out
    # all three columns are width 7.
    agents = [
        {"status": "ok", "tokens_in": 2000,    "tokens_out": 5,    "tokens_cached": 100,    "description": "z"},
        {"status": "ok", "tokens_in": 500,     "tokens_out": 10,   "tokens_cached": 200,    "description": "z"},
        {"status": "ok", "tokens_in": 100,     "tokens_out": 1234, "tokens_cached": 1234567,"description": "z"},
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    lines = out.split("\n")
    agent_lines = lines[5:]

    # All three rows share the same prefix "[ok]  z  " (description "z"),
    # so column END positions are identical across rows. Right-alignment is
    # verified by checking that all three lines have the SAME LENGTH (every
    # cell ends at the same column index, the right edge of the line).
    line_lengths = [len(line) for line in agent_lines]
    assert len(set(line_lengths)) == 1, (
        f"agent lines have different lengths (not right-aligned): {line_lengths}"
    )

    # Each cell has width _TOKEN_COLUMN_WIDTH=7 (max of label length,
    # formatted value length, and the column-width floor). Cell section
    # = w_in + 1 + w_out + 1 + w_cached chars, located at the END of
    # each line. We can extract each cell by slicing that section at
    # offsets derived from the column width, so the test tracks the
    # constant instead of hardcoding "23" or "7".
    cell_section = agent_lines[0][-(_TOKEN_COLUMN_WIDTH * 3 + 2):]
    in_cell = cell_section[0:_TOKEN_COLUMN_WIDTH]
    out_cell = cell_section[_TOKEN_COLUMN_WIDTH + 1 : _TOKEN_COLUMN_WIDTH * 2 + 1]
    cached_cell = cell_section[_TOKEN_COLUMN_WIDTH * 2 + 2 :]
    assert len(in_cell) == _TOKEN_COLUMN_WIDTH, f"in cell width != {_TOKEN_COLUMN_WIDTH}: {in_cell!r}"
    assert len(out_cell) == _TOKEN_COLUMN_WIDTH, f"out cell width != {_TOKEN_COLUMN_WIDTH}: {out_cell!r}"
    assert len(cached_cell) == _TOKEN_COLUMN_WIDTH, f"cached cell width != {_TOKEN_COLUMN_WIDTH}: {cached_cell!r}"


# ---------------------------------------------------------------------------
# long description truncated with U+2026
# ---------------------------------------------------------------------------

def test_long_description_truncated() -> None:
    """Description with 60 chars → truncated to 40 chars with U+2026 ellipsis
    as the last char of the description portion."""
    header = "Session: x"
    long_desc = "A" * 60
    assert len(long_desc) == 60
    agents = [
        {
            "status": "ok",
            "tokens_in": 50,
            "tokens_out": 30,
            "tokens_cached": 20,
            "description": long_desc,
        },
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    lines = out.split("\n")
    # header + table header + start + sum + main + 1 agent = 6
    agent_line = lines[5]

    assert agent_line.startswith(_TABLE_ROW_PREFIX + "[ok]")
    # description portion ends with U+2026
    assert "…" in agent_line, f"ellipsis missing from line {agent_line!r}"
    # The description column runs from after the status tag prefix up
    # to (but not including) the _DESC_TOKEN_GAP separator. We exclude
    # both the prefix and the trailing cell-section by computing the
    # cell section size from _TOKEN_COLUMN_WIDTH and adding
    # _DESC_TOKEN_GAP length. Slicing this way avoids splitting on
    # _DESC_TOKEN_GAP, which would mis-split on a description
    # containing internal double-space runs.
    prefix = _TABLE_ROW_PREFIX + f"{'[ok]':<{_ICON_COL_WIDTH}}" + _STATUS_GAP
    cell_section_len = _TOKEN_COLUMN_WIDTH * 3 + 2  # 3 cells + 2 separators
    desc_part = agent_line[len(prefix) : -(cell_section_len + len(_DESC_TOKEN_GAP))]
    assert len(desc_part) <= 40, (
        f"description width {len(desc_part)} > 40: {desc_part!r}"
    )
    assert desc_part.endswith("…"), (
        f"description not ellipsised: {desc_part!r}"
    )


# ---------------------------------------------------------------------------
# sum calculation: aggregates breakdown values, NOT a single total
# ---------------------------------------------------------------------------

def test_sum_calculation() -> None:
    """sum row aggregates per-column breakdown: in = main_in + sum(agent
    tokens_in), out = main_out + sum(agent tokens_out), cached = main_cached
    + sum(agent tokens_cached)."""
    header = "Session: x"
    agents = [
        {"status": "ok", "tokens_in": 100, "tokens_out": 10, "tokens_cached": 5, "description": "a"},
        {"status": "ok", "tokens_in": 200, "tokens_out": 20, "tokens_cached": 15, "description": "b"},
    ]
    # main: in=50, out=30, cached=10
    # sum: in=350, out=60, cached=30

    out = render_output(header, 0, 0, 0, _main(50, 30, 10), agents)
    lines = out.split("\n")
    sum_line = lines[3]

    # sum in = 350, format_tokens(350) = "350"
    assert sum_line.startswith(_TABLE_ROW_PREFIX + "sum:")
    assert "350" in sum_line  # in column
    assert "60" in sum_line   # out column
    assert "30" in sum_line   # cached column


# ---------------------------------------------------------------------------
# sum aggregates all rows
# ---------------------------------------------------------------------------

def test_sum_aggregates_all_rows() -> None:
    """sum row = main + every agent (no agent excluded) — even agents with
    zero breakdown contribute zero, not skipped."""
    header = "Session: x"
    agents = [
        # 4 agents; tokens_out values: 0, 100, 200, 300 → sum=600
        {"status": "ok",  "tokens_in": 0,   "tokens_out": 0,   "tokens_cached": 0,   "description": "a"},
        {"status": "ok",  "tokens_in": 0,   "tokens_out": 100, "tokens_cached": 0,   "description": "b"},
        {"status": "err", "tokens_in": 0,   "tokens_out": 200, "tokens_cached": 0,   "description": "c"},
        {"status": "run", "tokens_in": 0,   "tokens_out": 300, "tokens_cached": 0,   "description": "d"},
    ]
    # main_out = 50 → sum_out = 50+0+100+200+300 = 650

    out = render_output(header, 0, 0, 0, _main(0, 50, 0), agents)
    lines = out.split("\n")
    sum_line = lines[3]

    # 650 < 1000 so format_tokens gives "650"
    assert "650" in sum_line, (
        f"expected sum '650' in sum line {sum_line!r}"
    )


# ---------------------------------------------------------------------------
# run-agent shows current values, not skipped
# ---------------------------------------------------------------------------

def test_run_agent_shows_current_values() -> None:
    """Status 'run' (mid-flow) agent renders with the current breakdown
    values — NOT skipped, NOT zeroed out."""
    header = "Session: x"
    agents = [
        {
            "status": "run",
            "tokens_in": 2500,
            "tokens_out": 800,
            "tokens_cached": 1500,
            "description": "Working on it",
        },
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    lines = out.split("\n")
    agent_line = lines[5]

    assert agent_line.startswith(_TABLE_ROW_PREFIX + "[run]")
    assert "Working on it" in agent_line
    # 2500 → round(2.5)=2 (banker's) → "2K"
    # 800 → "800"
    # 1500 → round(1.5)=2 → "2K"
    # Both in and cached collapse to "2K"; exactly two "2K" substrings
    # must appear in the line (one for in, one for cached). Verify the
    # count to guard against silent format regressions where only one
    # field collapses.
    assert agent_line.count("2K") == 2, (
        f"expected exactly 2 '2K' substrings (in=2500 + cached=1500), "
        f"got {agent_line.count('2K')} in line {agent_line!r}"
    )
    assert "800" in agent_line


# ---------------------------------------------------------------------------
# [kill] status rendering (added per 20260824-subagent-status-via-queue-notifications)
# ---------------------------------------------------------------------------

def test_kill_status_renders_as_kill_tag() -> None:
    """Agent with status='kill' → line starts with '[kill]' tag, identical
    shape to [ok]/[err]/[stop]/[run] lines (tabular format)."""
    header = "Session: x"
    agents = [
        {
            "status": "kill",
            "tokens_in": 100,
            "tokens_out": 50,
            "tokens_cached": 20,
            "description": "agent killed mid-flight",
        },
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    lines = out.split("\n")
    agent_line = lines[5]

    assert agent_line.startswith(_TABLE_ROW_PREFIX + "[kill]")
    assert "agent killed mid-flight" in agent_line
    assert "100" in agent_line
    assert "50" in agent_line
    assert "20" in agent_line


def test_kill_status_zero_breakdown_renders_zeros() -> None:
    """Agent with status='kill' and no breakdown data → '[kill]' tag + three
    zero cells (consistent with the all-zero render for any other status)."""
    header = "Session: x"
    agents = [
        {
            "status": "kill",
            "description": "killed before tokens",
        },
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    lines = out.split("\n")
    agent_line = lines[5]

    assert agent_line.startswith(_TABLE_ROW_PREFIX + "[kill]")
    assert "killed before tokens" in agent_line
    # Cells are right-padded zeros — three "0"s in the trailing cell section.
    cell_section = agent_line[-23:]
    for cell in (cell_section[0:7], cell_section[8:15], cell_section[16:23]):
        assert cell.strip() == "0", (
            f"zero cell expected, got {cell!r}"
        )


def test_unknown_status_renders_as_question_mark() -> None:
    """Defensive: an unknown status value (not in _STATUSES tuple) surfaces
    as '[?]' rather than failing. Pre-existing behavior, regression check."""
    from status_line import _STATUSES

    assert "kill" in _STATUSES, (
        f"kill must be in _STATUSES; got {_STATUSES}"
    )
    assert set(_STATUSES) == {"ok", "run", "err", "stop", "kill"}, (
        f"_STATUSES unexpected: {_STATUSES}"
    )

    header = "Session: x"
    agents = [
        {
            "status": "weird-state",
            "tokens_in": 100,
            "tokens_out": 0,
            "tokens_cached": 0,
            "description": "x",
        },
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    lines = out.split("\n")
    agent_line = lines[5]

    assert agent_line.startswith(_TABLE_ROW_PREFIX + "[?]")


# ---------------------------------------------------------------------------
# agent with all-zero breakdown — still rendered (not skipped)
# ---------------------------------------------------------------------------

def test_agent_no_assistant_events_renders_zeros() -> None:
    """Agent with no breakdown data (None or missing fields) renders three
    zeros. The line is NOT skipped — the agent is still listed."""
    header = "Session: x"
    agents = [
        # missing all three breakdown fields entirely (None) — should
        # still render as "0 0 0"
        {"status": "run", "description": "no events yet"},
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    lines = out.split("\n")
    # header + table header + start + sum + main + 1 agent = 6
    agent_line = lines[5]

    assert agent_line.startswith(_TABLE_ROW_PREFIX + "[run]")
    assert "no events yet" in agent_line
    # Three right-aligned "0" cells of width _TOKEN_COLUMN_WIDTH=7. With
    # all values being 0, format_tokens gives "0" (1 char) and the column
    # width is 7. Cell section = 7+1+7+1+7 = 23 chars at the END of the
    # line. Derived from _TOKEN_COLUMN_WIDTH so the test tracks the
    # constant instead of hardcoding 23.
    cell_section_len = _TOKEN_COLUMN_WIDTH * 3 + 2
    cell_section = agent_line[-cell_section_len:]
    in_cell = cell_section[0:_TOKEN_COLUMN_WIDTH]
    out_cell = cell_section[_TOKEN_COLUMN_WIDTH + 1 : _TOKEN_COLUMN_WIDTH * 2 + 1]
    cached_cell = cell_section[_TOKEN_COLUMN_WIDTH * 2 + 2 :]
    # Each cell is right-aligned: "0" right-padded to width 7 = 6 spaces + "0".
    for cell in (in_cell, out_cell, cached_cell):
        assert cell.endswith("0"), (
            f"cell does not end with '0': {cell!r}"
        )
        assert cell.strip() == "0", (
            f"cell is not zero: {cell!r}"
        )


# ---------------------------------------------------------------------------
# large values format as k/M (not raw digits)
# ---------------------------------------------------------------------------

def test_large_values_format_as_k() -> None:
    """2000 input_tokens renders as '2K', not '2000'. format_tokens is
    applied BEFORE :>W, not after."""
    header = "Session: x"
    agents = [
        {"status": "ok", "tokens_in": 2000, "tokens_out": 0, "tokens_cached": 0, "description": "big"},
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    lines = out.split("\n")
    agent_line = lines[5]

    # The "in" cell of the agent line should contain "2K" (formatted) and
    # NOT contain the literal "2000" as a substring (which would mean
    # format_tokens wasn't applied before :>W).
    assert "2K" in agent_line, (
        f"expected formatted '2K' in agent line {agent_line!r}"
    )
    assert "2000" not in agent_line, (
        f"raw '2000' should have been formatted: {agent_line!r}"
    )


# ---------------------------------------------------------------------------
# table header row — three labels right-aligned under their columns
# ---------------------------------------------------------------------------

def test_table_header_row() -> None:
    """Second line (after header) contains the token labels in/out/cached
    followed by the always-visible work/wait/total block, each right-
    aligned within its own column width. The widths match what sum/main/
    agent rows use, so the labels line up with the cells below."""
    header = "Session: x"
    agents = [
        {"status": "ok", "tokens_in": 50000, "tokens_out": 200, "tokens_cached": 700, "description": "a"},
    ]

    out = render_output(header, 0, 0, 0, _main(1000, 0, 0), agents)
    lines = out.split("\n")
    table_header = lines[1]

    # all six labels present (in/out/cached + the always-visible time block)
    for label in ("in", "out", "cached", "work", "wait", "total"):
        assert label in table_header
    # carries the table-row prefix, and is not a sum/main/start line
    assert table_header.startswith(_TABLE_ROW_PREFIX)
    assert not table_header.startswith(_TABLE_ROW_PREFIX + "sum:")
    assert not table_header.startswith(_TABLE_ROW_PREFIX + "main:")
    assert not table_header.startswith(_TABLE_ROW_PREFIX + "start:")

    # The table header carries the "| " prefix followed by exactly the
    # three token labels separated by single spaces, each right-aligned to
    # the column width, padded on the left by `w_desc + _ICON_COL_WIDTH +
    # 4` spaces (the prefix width that agent rows also use, after the icon
    # column is padded to a fixed width); the time block trails after the
    # wide description gap, its labels right-aligned to the 8-char time
    # floor (empty data → no cell widening). We can verify by
    # reconstructing what the renderer would produce, using the
    # token-column width formula (former _col_width helper, inlined at the
    # top of this file) so the test tracks the formula rather than
    # recomputing it. The column value lists mirror render_output's:
    # start row + main row + agent rows.
    in_width = _col_width([0, 1000, 50000], "in")
    out_width = _col_width([0, 0, 200], "out")
    cached_width = _col_width([0, 0, 700], "cached")
    w_desc = max(len(a["description"]) for a in agents)
    header_pad = w_desc + _ICON_COL_WIDTH + 4
    expected_table_header = (
        f"{_TABLE_ROW_PREFIX}{' ' * header_pad}"
        f"{'in':>{in_width}} {'out':>{out_width}} {'cached':>{cached_width}}"
        f"{_DESC_TOKEN_GAP}{'work':>8} {'wait':>8} {'total':>8}"
    )
    assert table_header == expected_table_header, (
        f"table header mismatch: got {table_header!r}, expected "
        f"{expected_table_header!r}"
    )

    # Note: comprehensive format_tokens coverage lives in
    # tests/test_format_tokens.py — we don't re-assert it here to keep
    # this file focused on render_output's contract.


# ---------------------------------------------------------------------------
# unknown status → [?] fallback icon
# ---------------------------------------------------------------------------

def test_format_tokens_used_for_cell_values() -> None:
    """Sanity: format_tokens is what we expect — guards against accidental
    changes to the formatter that would silently break render_output."""
    # 1000 → "1K" (round to nearest k)
    assert format_tokens(1000) == "1K"
    # 999 < 1000 → "999"
    assert format_tokens(999) == "999"
    # 1_500_000 → "1.5M"
    assert format_tokens(1_500_000) == "1.5M"


def test_unknown_status_renders_question_mark_icon() -> None:
    """An agent with a status not in _STATUSES (e.g. data corruption,
    future-added state) renders as "[?]" instead of crashing. The renderer
    validates the status against _STATUSES — the source-of-truth list —
    rather than relying on callers to whitelist.
    """
    header = "Session: x"
    agents = [
        {
            "status": "unknown-state",
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cached": 0,
            "description": "future-state",
        },
    ]

    out = render_output(header, 0, 0, 0, _main(0, 0, 0), agents)
    agent_line = out.split("\n")[5]

    assert agent_line.startswith(_TABLE_ROW_PREFIX + "[?]"), (
        f"unknown status should render as [?], got: {agent_line!r}"
    )
    assert "future-state" in agent_line


# ---------------------------------------------------------------------------
# negative-number defensive path — clamps to 0 (format_tokens contract)
# ---------------------------------------------------------------------------

def test_negative_number_renders_as_zero() -> None:
    """format_tokens clamps negative values to "0" (defensive: status line
    must never display negative tokens). render_output relies on this
    rather than re-implementing the clamp."""
    header = "Session: x"
    agents = [
        {
            "status": "run",
            "tokens_in": -100,
            "tokens_out": -50,
            "tokens_cached": 0,
            "description": "sentinel-negatives",
        },
    ]

    out = render_output(header, 0, 0, 0, _main(-10, 0, 0), agents)
    lines = out.split("\n")
    main_line = lines[4]  # main row
    agent_line = lines[5]

    # main: -10 → "0" (clamped). Sum row: -10 + (-100) = -110 → "0".
    # Both must contain "0" as their formatted value; neither should
    # leak the negative sign or the raw digit.
    # Positive assertion: each clamped cell must render as "0". The main
    # row has 3 cells (in/out/cached), so expects 3 "0"s; the agent row
    # likewise has 3 cells and expects 3 "0"s (description "sentinel-
    # negatives" contains no digits).
    assert main_line.count("0") >= 3, (
        f"main row should contain three '0's for clamped cells, "
        f"got: {main_line!r}"
    )
    assert agent_line.count("0") >= 3, (
        f"agent row should contain three '0's for clamped in/out/cached, "
        f"got: {agent_line!r}"
    )
    assert "-10" not in main_line, (
        f"negative main_in should be clamped: {main_line!r}"
    )
    assert "-100" not in agent_line, (
        f"negative tokens_in should be clamped: {agent_line!r}"
    )
    assert "-50" not in agent_line, (
        f"negative tokens_out should be clamped: {agent_line!r}"
    )


# ---------------------------------------------------------------------------
# start row — first-message breakdown (first table row)
# ---------------------------------------------------------------------------

def test_start_row_is_first_table_row() -> None:
    """The start row renders right after the labels row (line 2), BEFORE
    sum/main, carrying the first message's in/out/cached values formatted
    via format_tokens (1000 → "1K")."""
    header = "Session: x"
    agents = [
        {"status": "ok", "tokens_in": 5, "tokens_out": 5, "tokens_cached": 5, "description": "a"},
    ]

    # start: in=1000→"1K", out=30→"30", cached=200→"200"
    out = render_output(header, 1000, 30, 200, _main(5000, 2000, 1000), agents)
    lines = out.split("\n")

    start_line = lines[2]
    assert start_line.startswith(_TABLE_ROW_PREFIX + "start:")
    # Cells formatted via format_tokens BEFORE right-alignment.
    assert "1K" in start_line, f"expected formatted '1K' in start row: {start_line!r}"
    assert "1000" not in start_line, f"raw '1000' should be formatted: {start_line!r}"
    assert "30" in start_line
    assert "200" in start_line
    # Row order: start (2) precedes sum (3) precedes main (4).
    assert lines[3].startswith(_TABLE_ROW_PREFIX + "sum:")
    assert lines[4].startswith(_TABLE_ROW_PREFIX + "main:")
    assert lines[2].startswith(_TABLE_ROW_PREFIX + "start:")


def test_start_row_not_included_in_sum() -> None:
    """sum = main + agents ONLY — the start row is a reference row and must
    NOT be added into the sum. Sentinel start values must not leak into the
    sum cells."""
    header = "Session: x"
    agents = [
        {"status": "ok", "tokens_in": 100, "tokens_out": 10, "tokens_cached": 5, "description": "a"},
    ]
    # main: in=50, out=30, cached=10 → sum: in=150, out=40, cached=15.
    # start is (900000, 900000, 900000): if it leaked into the sum, the sum
    # cells would show "900K" (format_tokens(900050)) instead of the small
    # values below. No correct sum cell contains "900".
    out = render_output(header, 900_000, 900_000, 900_000, _main(50, 30, 10), agents)
    lines = out.split("\n")
    sum_line = lines[3]

    assert sum_line.startswith(_TABLE_ROW_PREFIX + "sum:")
    assert "150" in sum_line, f"sum in must be main+agents (150), got: {sum_line!r}"
    assert "40" in sum_line, f"sum out must be 40, got: {sum_line!r}"
    assert "15" in sum_line, f"sum cached must be 15, got: {sum_line!r}"
    assert "900" not in sum_line, f"start sentinel leaked into sum row: {sum_line!r}"


def test_start_row_wide_value_expands_column() -> None:
    """A wide start value participates in the column-width computation: the
    start cell and the main cell right-align at the same column even when
    the start value is the widest cell in its column."""
    header = "Session: x"
    # 12_345_678_900 → "12345.7M" (8 chars > _TOKEN_COLUMN_WIDTH=7) — the
    # in-column must widen to 8 so the start cell does not overflow.
    wide_start_in = 12_345_678_900
    out = render_output(header, wide_start_in, 0, 0, _main(7, 0, 0), [])
    lines = out.split("\n")
    start_line, main_line = lines[2], lines[3]

    assert "12345.7M" in start_line, f"wide start value mangled: {start_line!r}"
    # The in-cell is the first of the three trailing cells; its END offset
    # from the line's right edge is w_out + 1 + w_cached (both 7 here → 15).
    # Right-alignment means both rows share that end offset.
    tail = _TOKEN_COLUMN_WIDTH * 2 + 1
    assert len(start_line) - len(start_line.rstrip()) == 0, "no trailing spaces expected"
    start_in_end = len(start_line) - tail
    main_in_end = len(main_line) - tail
    assert start_in_end == main_in_end, (
        f"in-column not aligned across start/main rows:\n"
        f"start: {start_line!r}\n main: {main_line!r}"
    )


# ---------------------------------------------------------------------------
# model + cost columns (prices present) — Task 4
# ---------------------------------------------------------------------------

_PRICES = {
    "glm-5.3@api.z.ai": {"in": 6.9, "out": 24.0, "cache": 1.7,
                         "per": 10000, "units": "credits"},
    "kimi-k3": {"in": 3.0, "out": 15.0, "cache": 0.3,
                "per": 1000000, "units": "$"},
}


def test_prices_columns_present_and_start_row_without_them() -> None:
    """With prices: the label row gains `model` and `cost`; the start row
    (a reference row) leaves both cells EMPTY; every table row carries
    the '| ' prefix."""
    header = "Session: x"
    main_models = {"glm-5.3": {"in": 10000, "out": 5000, "cached": 20000}}
    agents = [
        {
            "status": "ok",
            "tokens_in": 10,
            "tokens_out": 5,
            "tokens_cached": 2,
            "description": "a",
            "models": {"glm-5.3": {"in": 10, "out": 5, "cached": 2}},
        },
    ]

    out = render_output(header, 1, 2, 3, main_models, agents,
                        prices=_PRICES, host="api.z.ai")
    lines = out.split("\n")

    # every table row carries the "| " prefix
    assert all(line.startswith(_TABLE_ROW_PREFIX) for line in lines[1:]), (
        f"unprefixed table row in {lines[1:]!r}"
    )
    # label row: empty label column + model/in/out/cached/cost labels and
    # the always-visible work/wait/total time block
    assert lines[1].split() == [
        "|", "model", "in", "out", "cached", "cost", "work", "wait", "total",
    ], (
        f"label row: {lines[1]!r}"
    )
    # start row: model/cost cells are empty → split sees label + 3 tokens
    assert lines[2].split() == ["|", "start:", "1", "2", "3"], (
        f"start row should carry no model/cost: {lines[2]!r}"
    )
    # main cost from the @host entry:
    # (10000*6.9 + 5000*24 + 20000*1.7)/10000 = 223000/10000 = 22.3
    assert "22.3 credits" in out, f"expected '22.3 credits' in {out!r}"
    # agent cost: (10*6.9 + 5*24 + 2*1.7)/10000 = 0.01924 → "0.02 credits"
    assert "0.02 credits" in out, f"expected '0.02 credits' in {out!r}"


def test_multi_model_groups_first_row_labels_and_order() -> None:
    """sum/main/agent groups expand to one row per model. Label/icon/
    description appear only on the FIRST row of the group. Model order
    follows FIRST APPEARANCE (main dict order, then agent-only models) —
    kimi-k3 renders before glm-5.3 although "glm-5.3" sorts first, and
    MiniMax-M3 (agent-only) appends after main's models in the sum group."""
    main_models = {
        "kimi-k3": {"in": 2_000_000, "out": 100_000, "cached": 0},
        "glm-5.3": {"in": 100, "out": 10, "cached": 5},
    }
    agents = [
        {
            "status": "ok",
            "tokens_in": 350,
            "tokens_out": 20,
            "tokens_cached": 0,
            "description": "multi agent",
            "models": {
                "glm-5.3": {"in": 50, "out": 20, "cached": 0},
                "MiniMax-M3": {"in": 300, "out": 0, "cached": 0},
            },
        },
    ]

    out = render_output("Session: x", 0, 0, 0, main_models, agents,
                        prices=_PRICES, host="")
    lines = out.split("\n")

    # header + label + start + sum(3 models) + main(2) + agent(2) = 10
    assert len(lines) == 10, f"expected 10 lines, got {len(lines)}: {lines!r}"
    # sum group: merged per model (main first-appearance order, then the
    # agent-only MiniMax-M3), NO cross-model aggregation.
    assert lines[3].split() == ["|", "sum:", "kimi-k3", "2.0M", "100K", "0", "$7.5"], lines[3]
    assert lines[4].split() == ["|", "glm-5.3", "150", "30", "5", "n/a"], lines[4]
    assert lines[5].split() == ["|", "MiniMax-M3", "300", "0", "0", "n/a"], lines[5]
    # main group: one row per model, "main:" only on the first
    assert lines[6].split() == ["|", "main:", "kimi-k3", "2.0M", "100K", "0", "$7.5"], lines[6]
    assert lines[7].split() == ["|", "glm-5.3", "100", "10", "5", "n/a"], lines[7]
    # agent group: icon+description only on the first row; continuation
    # row carries just the model + cells.
    assert lines[8].startswith(_TABLE_ROW_PREFIX + "[ok]"), lines[8]
    assert "multi agent" in lines[8]
    assert lines[8].split()[-5:] == ["glm-5.3", "50", "20", "0", "n/a"], lines[8]
    assert lines[9].split() == ["|", "MiniMax-M3", "300", "0", "0", "n/a"], lines[9]
    assert "multi agent" not in lines[9], (
        f"description must not repeat on continuation rows: {lines[9]!r}"
    )
    # cross-model sum would produce in=450 (150+300) — must not exist
    assert "450" not in out, f"sum must stay per-model: {out!r}"


def test_zero_token_model_rows_skipped() -> None:
    """A per-model record with all-zero tokens (e.g. <synthetic>) is
    skipped entirely — it does not render a row."""
    main_models = {
        "glm-5.3": {"in": 100, "out": 10, "cached": 5},
        "<synthetic>": {"in": 0, "out": 0, "cached": 0},
    }
    out = render_output("Session: x", 0, 0, 0, main_models, [],
                        prices=_PRICES, host="")
    lines = out.split("\n")
    # header + label + start + ONE main row (glm only)
    assert len(lines) == 4, f"expected 4 lines, got {len(lines)}: {lines!r}"
    assert "<synthetic>" not in out, out
    assert "glm-5.3" in out


def test_all_zero_main_group_renders_single_zero_row() -> None:
    """A group whose every per-model record is zero (only <synthetic>)
    renders ONE row with zeros and an EMPTY model cell — the group is
    never skipped."""
    main_models = {"<synthetic>": {"in": 0, "out": 0, "cached": 0}}
    out = render_output("Session: x", 0, 0, 0, main_models, [],
                        prices=_PRICES, host="")
    lines = out.split("\n")
    assert len(lines) == 4, f"expected 4 lines, got {len(lines)}: {lines!r}"
    assert lines[3].split() == ["|", "main:", "0", "0", "0"], lines[3]


def test_agent_without_models_renders_zero_row_with_empty_model() -> None:
    """Agent with no events (no `models` key at all) → single row, zeros,
    EMPTY model cell — agents are never skipped."""
    agents = [
        {
            "status": "run",
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cached": 0,
            "description": "no events yet",
        },
    ]
    main_models = {"glm-5.3": {"in": 10, "out": 0, "cached": 0}}
    out = render_output("Session: x", 0, 0, 0, main_models, agents,
                        prices=_PRICES, host="")
    lines = out.split("\n")
    agent_line = lines[5]
    assert agent_line.startswith(_TABLE_ROW_PREFIX + "[run]"), agent_line
    assert "no events yet" in agent_line
    assert agent_line.split()[-3:] == ["0", "0", "0"], (
        f"agent row should end in three zero cells: {agent_line!r}"
    )


def test_agent_with_only_zero_models_renders_zero_row() -> None:
    """Agent whose per-model records are ALL zero (only <synthetic>)
    → single zero row with an empty model cell."""
    agents = [
        {
            "status": "err",
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cached": 0,
            "description": "synth only",
            "models": {"<synthetic>": {"in": 0, "out": 0, "cached": 0}},
        },
    ]
    main_models = {"glm-5.3": {"in": 10, "out": 0, "cached": 0}}
    out = render_output("Session: x", 0, 0, 0, main_models, agents,
                        prices=_PRICES, host="")
    lines = out.split("\n")
    assert "<synthetic>" not in out, out
    agent_line = lines[5]
    assert agent_line.startswith(_TABLE_ROW_PREFIX + "[err]"), agent_line
    assert "synth only" in agent_line
    assert agent_line.split()[-3:] == ["0", "0", "0"], agent_line


def test_sum_group_zero_fallback_row() -> None:
    """The sum group left with no rows after zero-skipping renders one
    zero row with an empty model cell (same invariant as main/agents)."""
    agents = [
        {
            "status": "ok",
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cached": 0,
            "description": "idle",
            "models": {"<synthetic>": {"in": 0, "out": 0, "cached": 0}},
        },
    ]
    out = render_output("Session: x", 0, 0, 0, {}, agents,
                        prices=_PRICES, host="")
    lines = out.split("\n")
    # header + label + start + sum(1 fallback) + main(1 fallback) + agent(1)
    assert len(lines) == 6, f"expected 6 lines, got {len(lines)}: {lines!r}"
    assert lines[3].split() == ["|", "sum:", "0", "0", "0"], lines[3]
    assert lines[4].split() == ["|", "main:", "0", "0", "0"], lines[4]


def test_host_key_matches_priced_model() -> None:
    """host='api.z.ai' resolves glm-5.3 via the 'glm-5.3@api.z.ai' entry;
    host='' has no plain glm-5.3 key → the same model renders 'n/a'."""
    main_models = {"glm-5.3": {"in": 10000, "out": 5000, "cached": 20000}}

    with_host = render_output("Session: x", 0, 0, 0, main_models, [],
                              prices=_PRICES, host="api.z.ai")
    assert "22.3 credits" in with_host, with_host
    assert "n/a" not in with_host, with_host

    without_host = render_output("Session: x", 0, 0, 0, main_models, [],
                                 prices=_PRICES, host="")
    assert "n/a" in without_host, without_host
    assert "22.3 credits" not in without_host, without_host


def test_no_prices_no_model_or_cost_columns() -> None:
    """prices=None (no prices.json) → both columns vanish: one row per
    group with the group totals, layout identical to the old render —
    even when main_models itself is multi-model."""
    main_models = {
        "glm-5.3": {"in": 100, "out": 10, "cached": 5},
        "kimi-k3": {"in": 200, "out": 20, "cached": 8},
    }
    out = render_output("Session: x", 0, 0, 0, main_models, [])
    lines = out.split("\n")
    assert len(lines) == 4, f"expected 4 lines, got {len(lines)}: {lines!r}"
    assert lines[1].split() == [
        "|", "in", "out", "cached", "work", "wait", "total",
    ], (
        f"no model/cost labels expected — but the time block is always "
        f"visible: {lines[1]!r}"
    )
    # single main row carrying the summed totals (300/30/13)
    assert lines[3].split() == ["|", "main:", "300", "30", "13"], lines[3]
    assert "glm-5.3" not in out and "kimi-k3" not in out


# ---------------------------------------------------------------------------
# with-prices layout — byte-exact alignment (review follow-up: every other
# prices test used .split(), which collapses all padding, so a wrong gap,
# swapped align or wrong floor could regress undetected)
# ---------------------------------------------------------------------------

def test_prices_layout_byte_exact() -> None:
    """The full with-prices layout pinned to exact strings: model column
    LEFT-aligned between the label column and `in` (2-space gaps on both
    sides), token columns RIGHT-aligned with the _TOKEN_COLUMN_WIDTH floor
    (single-space separators), cost RIGHT-aligned after `cached` (2-space
    gap), and the always-visible time block closing every row — labels at
    their 8-char floor, block opened by the wide units-column gap, values
    (when present) right-aligned HH:MM:SS cells riding ONLY a group's
    FIRST row; the start row never carries time cells. All rows share the
    column x-positions computed over every cell, including the label
    row."""
    main_models = {"glm-5.3": {"in": 10000, "out": 5000, "cached": 20000}}
    agents = [
        {
            "status": "ok",
            "tokens_in": 2_000_010,
            "tokens_out": 100_005,
            "tokens_cached": 2,
            "description": "a",
            "models": {
                "glm-5.3": {"in": 10, "out": 5, "cached": 2},
                "kimi-k3": {"in": 2_000_000, "out": 100_000, "cached": 0},
            },
            # transient orchestrator-injected durations
            "time_work": 4350.0,
            "time_wait": 180.0,
            "time_total": 4530.0,
        },
    ]
    out = render_output(
        "Session: x", 1, 2, 3, main_models, agents,
        prices=_PRICES, host="api.z.ai",
        main_time=(101_000.0, 500.0, 101_500.0),
    )
    assert out.split("\n") == [
        "Session: x",
        "|            model         in     out  cached"
        "  cost              work     wait    total",
        "| start:                    1       2       3",
        "| sum:       glm-5.3      10K      5K     20K"
        "  22.3 credits  28:03:20 00:08:20 28:11:40",
        "|            kimi-k3     2.0M    100K       0  $7.5",
        "| main:      glm-5.3      10K      5K     20K"
        "  22.3 credits  28:03:20 00:08:20 28:11:40",
        "| [ok]    a  glm-5.3       10       5       2"
        "  0.02 credits  01:12:30 00:03:00 01:15:30",
        "|            kimi-k3     2.0M    100K       0  $7.5",
    ], out


def test_readme_examples_match_render_output() -> None:
    """Both README example blocks are pinned byte-for-byte: the documented
    scenario is reconstructed here, run through the real render_output and
    diffed against the fenced blocks in README.md. The header line keeps
    its <sid>/<git-branch>/<model> placeholders — render_output passes the
    header through verbatim, so it matches too. Guards against example
    drift (the README example had silently diverged from the real render
    once before — commit a9cc75a)."""
    readme_path = Path(__file__).resolve().parent.parent / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```[a-z]*\n(.*?)```", readme, flags=re.DOTALL)
    with_prices_block = next(b for b in blocks if "| sum:" in b and "cost" in b)
    no_prices_block = next(b for b in blocks if "| sum:" in b and "cost" not in b)

    header = (
        "Session: <sid> | Branch: <git-branch> | Model: <model> | "
        "User: n/a | Context: 215K (107%)"
    )
    main_models = {
        "glm-5.3": {"in": 1_100_000, "out": 30_000, "cached": 50_700_000},
        "kimi-k3": {"in": 150_000, "out": 40_000, "cached": 3_000_000},
    }
    # Transient duration triples exactly as _main_unsafe injects them post-
    # cache-write (seconds; work + wait == total per agent and session).
    session_time = (101_000.0, 500.0, 101_500.0)
    agents = [
        {
            "status": "ok",
            "tokens_in": 12_000,
            "tokens_out": 4_000,
            "tokens_cached": 100_000,
            "description": "Review: implementation plan",
            "models": {"glm-5.3": {"in": 12_000, "out": 4_000, "cached": 100_000}},
            "time_work": 4350.0,
            "time_wait": 180.0,
            "time_total": 4530.0,
        },
        {
            "status": "err",
            "tokens_in": 500,
            "tokens_out": 200,
            "tokens_cached": 3_000,
            "description": "Review: quality",
            "models": {"MiniMax-M3": {"in": 500, "out": 200, "cached": 3_000}},
            "time_work": 1600.0,
            "time_wait": 300.0,
            "time_total": 1900.0,
        },
        {
            "status": "run",
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cached": 0,
            "description": "Task 4: MissingGlyphLog",
            "models": {},
            "time_work": 124.0,
            "time_wait": 0.0,
            "time_total": 124.0,
        },
    ]

    with_prices = render_output(
        header, 12_000, 1_000, 0, main_models, agents,
        prices=_PRICES, host="api.z.ai", start_model="glm-5.3",
        main_time=session_time,
    )
    assert with_prices.split("\n") == with_prices_block.rstrip("\n").split("\n"), (
        "README with-prices example drifted from the real render_output"
    )

    no_prices = render_output(
        header, 12_000, 1_000, 0, main_models, agents, main_time=session_time
    )
    assert no_prices.split("\n") == no_prices_block.rstrip("\n").split("\n"), (
        "README no-prices example drifted from the real render_output"
    )


def test_start_row_carries_model_and_cost() -> None:
    """In prices mode the start row carries the first event's model and its
    priced cost; with no start_model (no usage-bearing first event /
    pre-upgrade cache) both cells render empty."""
    main_models = {"glm-5.3": {"in": 10_000, "out": 5_000, "cached": 20_000}}
    priced = render_output(
        "Session: x", 10_000, 5_000, 20_000, main_models, [],
        prices=_PRICES, host="api.z.ai", start_model="glm-5.3",
    )
    # (10000*6.9 + 5000*24 + 20000*1.7) / 10000 = 22.3 credits
    assert priced.split("\n")[2].split() == [
        "|", "start:", "glm-5.3", "10K", "5K", "20K", "22.3", "credits",
    ], priced.split("\n")[2]

    unpriced_model = render_output(
        "Session: x", 10_000, 5_000, 20_000, main_models, [],
        prices=_PRICES, host="api.z.ai", start_model="MiniMax-M3",
    )
    assert unpriced_model.split("\n")[2].split()[-1] == "n/a"

    no_model = render_output(
        "Session: x", 10_000, 5_000, 20_000, main_models, [],
        prices=_PRICES, host="api.z.ai",
    )
    assert no_model.split("\n")[2].split() == [
        "|", "start:", "10K", "5K", "20K",
    ], no_model.split("\n")[2]

    # prices=None: start row keeps the historical 3-cell shape regardless.
    legacy = render_output(
        "Session: x", 10_000, 5_000, 20_000, main_models, [],
        start_model="glm-5.3",
    )
    assert legacy.split("\n")[2].split() == [
        "|", "start:", "10K", "5K", "20K",
    ], legacy.split("\n")[2]


def test_prices_empty_dict_shows_columns_with_na() -> None:
    """prices={} (a valid but EMPTY prices file — load_prices returns {}
    for `[]`) still shows the model/cost columns; every priced-model
    lookup fails → all cost cells render n/a. Documents the decision that
    the column gate is `prices is None`, not falsiness."""
    out = render_output(
        "Session: x", 0, 0, 0, {"glm-5.3": {"in": 10, "out": 0, "cached": 0}},
        [], prices={}, host="",
    )
    lines = out.split("\n")
    assert lines[1].split() == [
        "|", "model", "in", "out", "cached", "cost", "work", "wait", "total",
    ], lines[1]
    assert lines[3].split() == ["|", "main:", "glm-5.3", "10", "0", "0", "n/a"], lines[3]


def test_empty_model_key_row_renders_tokens_with_empty_cells() -> None:
    """An assistant event with usage but NO model field accumulates under
    the "" key; the row renders the tokens with an EMPTY model cell and an
    EMPTY cost cell (not n/a — no model means nothing to price)."""
    out = render_output(
        "Session: x", 0, 0, 0, {"": {"in": 100, "out": 10, "cached": 5}},
        [], prices=_PRICES, host="",
    )
    lines = out.split("\n")
    # 100/10/5 cells present, but no model id and no cost between them
    assert lines[3].split() == ["|", "main:", "100", "10", "5"], lines[3]
    assert "n/a" not in lines[3], lines[3]


def test_sum_row_widens_column_when_its_cell_is_widest() -> None:
    """[pinned decision] The sum row's cells participate in the column-
    width computation (unlike the pre-model-columns render, which excluded
    sum). At extreme totals (sum > every component) the token column
    widens to the sum cell and all rows right-align to the same edge."""
    five_g = 5_000_000_000  # format_tokens → "5000.0M" (7 chars)
    main_models = {"glm-5.3": {"in": five_g, "out": 0, "cached": 0}}
    agents = [
        {
            "status": "ok",
            "tokens_in": five_g,
            "tokens_out": 0,
            "tokens_cached": 0,
            "description": "a",
            "models": {"glm-5.3": {"in": five_g, "out": 0, "cached": 0}},
        },
    ]
    out = render_output("Session: x", 0, 0, 0, main_models, agents)
    lines = out.split("\n")
    # sum in = 10G → "10000.0M" (8 chars) — the widest in-cell → column 8.
    assert lines[3].split() == ["|", "sum:", "10000.0M", "0", "0"], lines[3]
    # Every row's in-cell occupies the same 8-char slice (right-aligned
    # against the sum-widened column): start "       0", sum "10000.0M",
    # main/agent " 5000.0M". Slice = everything left of the out column:
    # gap + out(7) + gap + cached(7).
    tail = _TOKEN_COLUMN_WIDTH * 2 + 2
    expected_cells = ["       0", "10000.0M", " 5000.0M", " 5000.0M"]
    for line, expected in zip(lines[2:], expected_cells):
        in_cell = line[len(line) - tail - 8 : len(line) - tail]
        assert in_cell == expected, (
            f"in-column not aligned at width 8: {line!r} cell {in_cell!r}"
        )


def test_corrupt_cache_records_render_without_raising() -> None:
    """A hand-corrupted cache can put None / non-numeric strings / non-dict
    values into per-model records. render_output must degrade the bad
    record (coerce to 0 / skip) instead of raising — an exception here is
    swallowed by main()'s catch-all and would replace the whole table with
    the fallback header on EVERY tick (the cache key still matches, so it
    would never self-heal)."""
    main_models = {
        "glm-5.3": None,  # non-dict record → skipped
        "kimi-k3": {"in": 10, "out": None, "cached": "oops"},  # coerced
        "bad": "not-a-dict",  # skipped
    }
    agents = [
        {
            "status": "ok",
            "tokens_in": None,  # flat fields corrupted too
            "tokens_out": "x",
            "tokens_cached": 5,
            "description": "a",
            "models": {"m": {"in": None, "out": "x", "cached": 5}},
        },
    ]
    out = render_output(
        "Session: x", 0, 0, 0, main_models, agents, prices=_PRICES, host=""
    )
    lines = out.split("\n")
    # kimi-k3 record survives with coerced zeros; corrupt siblings skipped.
    assert lines[3].split() == ["|", "sum:", "kimi-k3", "10", "0", "0", "$0.00"], lines[3]
    assert lines[4].split() == ["|", "m", "0", "0", "5", "n/a"], lines[4]
    assert "glm-5.3" not in out and "bad" not in out, out

    # The prices=None path (_models_total) must tolerate the same corrupt
    # records: main totals = 10/0/0 (kimi-k3 only), agent flat fields
    # coerced from None/"x" to 0.
    out_flat = render_output("Session: x", 0, 0, 0, main_models, agents)
    flat_lines = out_flat.split("\n")
    # sum = main(10/0/0) + agent(0/0/5) = 10/0/5.
    assert flat_lines[3].split() == ["|", "sum:", "10", "0", "5"], flat_lines[3]
    assert flat_lines[4].split() == ["|", "main:", "10", "0", "0"], flat_lines[4]


# ---------------------------------------------------------------------------
# time columns — work/wait/total (plan 20260827-status-line-time-columns)
# ---------------------------------------------------------------------------

# Session union triple: 101000s / 500s / 101500s.
# format_duration → 28:03:20 / 00:08:20 / 28:11:40 (and 28:03:20 + 00:08:20
# == 28:11:40, the work + wait == total invariant the orchestrator keeps).
_SESSION_TIME = (101_000.0, 500.0, 101_500.0)
_WORK_WAIT_TOTAL = ["28:03:20", "00:08:20", "28:11:40"]

# Agent lifetime triple: 4350s / 180s / 4530s (work + wait == total).
_AGENT_TIME_KEYS = {
    "time_work": 4350.0,
    "time_wait": 180.0,
    "time_total": 4530.0,
}
_AGENT_WORK_WAIT_TOTAL = ["01:12:30", "00:03:00", "01:15:30"]


def test_time_labels_in_both_modes() -> None:
    """Columns are ALWAYS visible: both layouts (with and without prices)
    close the header row with the work/wait/total labels."""
    out_plain = render_output("Session: x", 0, 0, 0, _main(0, 0, 0), [])
    assert out_plain.split("\n")[1].split() == [
        "|", "in", "out", "cached", "work", "wait", "total",
    ], out_plain.split("\n")[1]

    out_prices = render_output(
        "Session: x", 0, 0, 0,
        {"glm-5.3": {"in": 10, "out": 0, "cached": 0}}, [],
        prices=_PRICES, host="api.z.ai",
    )
    assert out_prices.split("\n")[1].split() == [
        "|", "model", "in", "out", "cached", "cost", "work", "wait", "total",
    ], out_prices.split("\n")[1]


def test_time_columns_order_and_placement() -> None:
    """Placement: plain mode opens the time block right after `cached` with
    the wide _DESC_TOKEN_GAP separator (the cached column's own gap), single
    spaces inside the block. Prices-mode placement is pinned byte-exact by
    test_prices_layout_byte_exact below."""

    # Plain mode, distinctive cells so the tail is unambiguous.
    out = render_output("Session: x", 12_345_678_900, 2, 3,
                        _main(0, 0, 0), [], main_time=_SESSION_TIME)
    main_line = out.split("\n")[3]
    expected_tail = (
        "0".rjust(_TOKEN_COLUMN_WIDTH)          # cached cell
        + _DESC_TOKEN_GAP                       # block opener
        + _WORK_WAIT_TOTAL[0].rjust(8) + " "    # work
        + _WORK_WAIT_TOTAL[1].rjust(8) + " "    # wait
        + _WORK_WAIT_TOTAL[2]                   # total (trailing gap rstripped)
    )
    assert main_line.endswith(expected_tail), (
        f"expected ...{expected_tail!r}, got ...{main_line[-60:]!r}"
    )


def test_time_floor_eight_and_widening() -> None:
    """Empty time data still reserves the 8-char floor per column ("HH:MM:SS"
    never wraps); a wider cell ("103:25:10", 9 chars) widens the column and
    the LABEL row's padding follows."""
    out = render_output("Session: x", 0, 0, 0, _main(0, 0, 0), [])
    labels = out.split("\n")[1]
    assert labels.endswith(f"{'work':>8} {'wait':>8} {'total':>8}"), labels

    wide = render_output("Session: x", 0, 0, 0, _main(0, 0, 0), [],
                         main_time=(0.0, 0.0, 372_450.0))
    wide_lines = wide.split("\n")
    # 372450s == 103h 27m 30s — one char beyond the floor.
    assert "103:27:30" in wide_lines[-1], wide_lines[-1]
    assert "372450" not in wide, "raw seconds must never leak into the render"
    # Zeros ARE legitimate duration values (a rendered 0-second wait);
    # only MISSING data hides behind "".
    assert "00:00:00" in wide_lines[-1], wide_lines[-1]
    # Per-column independence (same rule as the token columns): ONLY the
    # widened column grows — total to 9, work/wait stay at the floor.
    assert wide_lines[1].endswith(f"{'work':>8} {'wait':>8} {'total':>9}"), (
        wide_lines[1]
    )


def test_start_row_time_cells_always_empty() -> None:
    """start: is a reference row — its work/wait/total cells stay EMPTY even
    when the session triple renders onto sum:/main:."""
    out = render_output("Session: x", 1000, 30, 200, _main(0, 0, 0), [],
                        main_time=_SESSION_TIME)
    start = out.split("\n")[2]
    assert start.split() == ["|", "start:", "1K", "30", "200"], start
    assert "28:" not in start, start


def test_main_time_renders_on_sum_and_main_rows() -> None:
    """The session's union triple lands on BOTH the sum: and main: rows
    (identical by construction — waiting on agents is main's work), as
    right-aligned HH:MM:SS cells in cell order work/wait/total."""
    agents = [
        {"status": "ok", "tokens_in": 100, "tokens_out": 10,
         "tokens_cached": 5, "description": "a"},
        {"status": "err", "tokens_in": 200, "tokens_out": 20,
         "tokens_cached": 15, "description": "b"},
    ]
    out = render_output("Session: x", 0, 0, 0, _main(50, 30, 10), agents,
                        main_time=_SESSION_TIME)
    lines = out.split("\n")
    # Legacy token cells are untouched; the three time cells trail them.
    assert lines[3].startswith(_TABLE_ROW_PREFIX + "sum:")
    assert lines[3].split()[:5] == ["|", "sum:", "350", "60", "30"], lines[3]
    assert lines[3].split()[-3:] == _WORK_WAIT_TOTAL, lines[3]
    assert lines[4].split()[:5] == ["|", "main:", "50", "30", "10"], lines[4]
    assert lines[4].split()[-3:] == _WORK_WAIT_TOTAL, lines[4]


def test_agent_without_time_fields_empty_cells() -> None:
    """An agent with NO time_* keys (pre-upgrade cache, orchestrator
    degradation) renders EMPTY trailing cells — never "00:00:00" (absent
    timestamps mean unknown, not zero elapsed)."""
    agents = [{"status": "ok", "tokens_in": 1000, "tokens_out": 900,
               "tokens_cached": 300, "description": "x"}]
    out = render_output("Session: x", 0, 0, 0, _main(0, 0, 0), agents)
    agent_line = out.split("\n")[5]
    assert agent_line.split() == [
        "|", "[ok]", "x", "1K", "900", "300",
    ], agent_line
    assert "00:00:00" not in out, out


def test_partial_and_junk_time_values_render_empty() -> None:
    """A PARTIAL session triple fills only its present cells; None cells and
    non-numeric junk (untrusted-cache convention) coerce to "" without
    raising; sub-second durations truncate (0.4s → "00:00:00" — a REAL
    rendered value, distinct from "")."""
    agents = [{
        "status": "ok", "tokens_in": 100, "tokens_out": 0,
        "tokens_cached": 0, "description": "a",
        "time_work": None,
        "time_wait": "not-a-number",
        "time_total": 0.4,
    }]
    out = render_output("Session: x", 0, 0, 0, _main(0, 0, 0), agents,
                        main_time=(3600.0, None, None))
    lines = out.split("\n")
    # main: work fills ("01:00:00"), wait/total are None → empty, and the
    # line rstrips back to the work cell.
    assert lines[4].split()[-4:] == ["0", "0", "0", "01:00:00"], lines[4]
    # agent: wait is garbage → "", work is None → ""; total 0.4s TRUNCATES
    # to a real "00:00:00".
    assert lines[5].split() == [
        "|", "[ok]", "a", "100", "0", "0", "00:00:00",
    ], lines[5]


def test_agent_time_cells_first_group_row_only_prices_mode() -> None:
    """An agent's transient time_* triple rides ONLY the FIRST row of its
    prices-mode group; per-model continuation rows end at their cost cell.
    The sum group gets the SESSION triple (from main_time) the same way."""
    main_models = {"glm-5.3": {"in": 100, "out": 10, "cached": 5}}
    agents = [{
        "status": "ok", "tokens_in": 350, "tokens_out": 20,
        "tokens_cached": 0, "description": "multi",
        "models": {
            "glm-5.3": {"in": 50, "out": 20, "cached": 0},
            "kimi-k3": {"in": 2_000_000, "out": 100_000, "cached": 0},
        },
        **_AGENT_TIME_KEYS,
    }]
    out = render_output("Session: x", 0, 0, 0, main_models, agents,
                        prices=_PRICES, host="", main_time=_SESSION_TIME)
    lines = out.split("\n")
    # header + label + start + sum(glm, kimi) + main(glm) + agent(glm, kimi)
    assert len(lines) == 8, lines
    assert lines[3].split() == [
        "|", "sum:", "glm-5.3", "150", "30", "5", "n/a", *_WORK_WAIT_TOTAL,
    ], lines[3]
    assert lines[4].split() == [
        "|", "kimi-k3", "2.0M", "100K", "0", "$7.5",
    ], lines[4]
    assert lines[5].split() == [
        "|", "main:", "glm-5.3", "100", "10", "5", "n/a", *_WORK_WAIT_TOTAL,
    ], lines[5]
    assert lines[6].split() == [
        "|", "[ok]", "multi", "glm-5.3", "50", "20", "0", "n/a",
        *_AGENT_WORK_WAIT_TOTAL,
    ], lines[6]
    # Continuation row: label AND time cells both stay behind.
    assert lines[7].split() == [
        "|", "kimi-k3", "2.0M", "100K", "0", "$7.5",
    ], lines[7]


def test_main_time_shows_in_fallback_zero_row_prices_mode() -> None:
    """Even a zero-fallback group (every per-model record zero-skipped)
    carries the session triple on its single row — groups are never
    skipped, and durations belong to the GROUP, not its model rows."""
    out = render_output("Session: x", 0, 0, 0,
                        {"<synthetic>": {"in": 0, "out": 0, "cached": 0}},
                        [], prices=_PRICES, host="", main_time=_SESSION_TIME)
    lines = out.split("\n")
    assert lines[3].split() == [
        "|", "main:", "0", "0", "0", *_WORK_WAIT_TOTAL,
    ], lines[3]


def test_legacy_call_keeps_historical_tokens() -> None:
    """A direct render_output call WITHOUT time arguments ([deviation]
    backward-compat contract): every historical token is intact — rows,
    ordering, values — only the header gains the three always-visible
    labels; no "00:00:00" placeholders materialize from thin air."""
    agents = [{"status": "ok", "tokens_in": 300, "tokens_out": 400,
               "tokens_cached": 100, "description": "foo bar"}]
    out = render_output("Session: abc", 100, 30, 200,
                        _main(1000, 500, 200), agents)
    lines = out.split("\n")
    assert len(lines) == 6, lines
    assert lines[2].split() == ["|", "start:", "100", "30", "200"], lines[2]
    assert lines[3].split() == ["|", "sum:", "1K", "900", "300"], lines[3]
    assert lines[4].split() == ["|", "main:", "1K", "500", "200"], lines[4]
    assert lines[5].split() == [
        "|", "[ok]", "foo", "bar", "300", "400", "100",
    ], lines[5]
    assert "00:00:00" not in out, out


def test_malformed_main_time_argument_is_tolerated() -> None:
    """main_time is not a 3-element sequence (junk, wrong arity) → ALL
    session cells render empty. Strict-triple, defensive-degrade: the hook
    must never raise over a shaped-wrong argument."""
    for junk in (None, "x", (1,), (1, 2, 3, 4), {"work": 1}):
        out = render_output("Session: x", 0, 0, 0, _main(0, 0, 0), [],
                            main_time=junk)
        main_line = out.split("\n")[3]
        assert main_line.split() == ["|", "main:", "0", "0", "0"], (
            f"junk main_time {junk!r} leaked into {main_line!r}"
        )
