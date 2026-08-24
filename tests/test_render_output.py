"""Tests for render_output — assemble the multi-line status line string.

render_output(header, main_in, main_out, main_cached, agents) returns a
string built as:
    header
    <table header — labels "in" / "out" / "cached", right-aligned per column>
    sum: <in> <out> <cached>     # only if len(agents) > 0
    main: <in> <out> <cached>
    for each agent (in input order):
        "[<status>]  <description>  <in> <out> <cached>"

Every numeric cell is formatted through format_tokens() (so 1000 → "1k")
and right-aligned to a per-column width (max of label length, the widest
formatted cell value, and _TOKEN_COLUMN_WIDTH=7). Each column's width is
computed independently.

Status icons: "[ok]", "[run]", "[err]", "[stop]". Description column is
NOT padded to a fixed width — only the token columns are. Description
>40 chars is truncated with U+2026.

Line layout for a single-agent scenario:
    [0] header
    [1] table header (in / out / cached)
    [2] sum
    [3] main
    [4] agent
"""
from __future__ import annotations

from status_line import (
    _DESC_TOKEN_GAP,
    _STATUS_GAP,
    _TOKEN_COLUMN_WIDTH,
    _col_width,
    format_tokens,
    render_output,
)


# ---------------------------------------------------------------------------
# single ok agent
# ---------------------------------------------------------------------------

def test_single_ok_agent() -> None:
    """1 agent [ok] with breakdown → 5 lines: header, table header, sum, main,
    agent line. Each numeric cell formatted via format_tokens."""
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

    out = render_output(header, 1000, 500, 200, agents)
    lines = out.split("\n")

    # header + table header + sum + main + agent = 5
    assert len(lines) == 5
    assert lines[0] == header
    # table header line contains the three labels
    assert "in" in lines[1]
    assert "out" in lines[1]
    assert "cached" in lines[1]
    # sum line: in=1300→"1k", out=900→"900", cached=300→"300"
    assert lines[2].startswith("sum:")
    assert "1k" in lines[2]
    assert "900" in lines[2]
    assert "300" in lines[2]
    # main line: 1000→"1k", 500→"500", 200→"200"
    assert lines[3].startswith("main:")
    assert "1k" in lines[3]
    assert "500" in lines[3]
    assert "200" in lines[3]
    # agent line: starts with [ok], contains description and three numbers
    assert lines[4].startswith("[ok]")
    assert "Task 1: foo" in lines[4]
    assert "300" in lines[4]
    assert "400" in lines[4]
    assert "100" in lines[4]


# ---------------------------------------------------------------------------
# zero agents — no sum line
# ---------------------------------------------------------------------------

def test_zero_agents_no_sum_line() -> None:
    """0 agents → header + table header + main only (no sum line)."""
    header = "Session: abc"
    out = render_output(header, 0, 42, 0, [])
    lines = out.split("\n")

    # header + table header + main = 3
    assert len(lines) == 3
    assert lines[0] == header
    # table header line follows
    assert "in" in lines[1] and "out" in lines[1] and "cached" in lines[1]
    # main line follows
    assert lines[2].startswith("main:")
    assert "42" in lines[2]
    # no "sum:" line at all
    assert "sum:" not in out


# ---------------------------------------------------------------------------
# 38 agents → 42 lines (header + table header + sum + main + 38 agents)
# ---------------------------------------------------------------------------

def test_38_agents_produce_42_lines() -> None:
    """38 agents → 42 lines: 1 header + 1 table header + 1 sum + 1 main + 38
    agent lines."""
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

    out = render_output(header, 5000, 2000, 1000, agents)
    lines = out.split("\n")

    assert len(lines) == 42
    assert lines[0] == header
    # table header is line 1
    assert "in" in lines[1] and "out" in lines[1] and "cached" in lines[1]
    assert lines[2].startswith("sum:")
    assert lines[3].startswith("main:")
    # remaining 38 lines all start with a status tag
    for line in lines[4:]:
        assert line.startswith("[")


# ---------------------------------------------------------------------------
# token alignment — right-aligned to a per-column fixed width
# ---------------------------------------------------------------------------

def test_token_alignment_right_aligned() -> None:
    """Each numeric column is right-aligned to its own fixed width, and
    integer values are formatted via format_tokens BEFORE padding to
    width (1234567 → "1.2M", 50000 → "50k", NOT the raw digits). This
    exercises the format_tokens-before-:>W rule with a wide range of
    magnitudes."""
    header = "Session: x"
    agents = [
        # tokens_in: 10, 50000, 1234567 — widest is "1.2M" (4 chars)
        {"status": "ok", "tokens_in": 10,      "tokens_out": 0, "tokens_cached": 0, "description": "a"},
        {"status": "ok", "tokens_in": 50000,   "tokens_out": 0, "tokens_cached": 0, "description": "b"},
        {"status": "ok", "tokens_in": 1234567, "tokens_out": 0, "tokens_cached": 0, "description": "c"},
    ]

    out = render_output(header, 0, 0, 0, agents)
    lines = out.split("\n")
    agent_lines = lines[4:]  # header + table header + sum + main + agents

    formatted_in = ["10", "50k", "1.2M"]
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
    # tokens_in widest=2000 → "2k" (2 chars)
    # tokens_out widest=1234 → "1k" (2 chars)
    # tokens_cached widest=1234567 → "1.2M" (4 chars)
    # widths: max(7, 2, len("1.2M")=4) = 7 for cached; 7 for in; 7 for out
    # all three columns are width 7.
    agents = [
        {"status": "ok", "tokens_in": 2000,    "tokens_out": 5,    "tokens_cached": 100,    "description": "z"},
        {"status": "ok", "tokens_in": 500,     "tokens_out": 10,   "tokens_cached": 200,    "description": "z"},
        {"status": "ok", "tokens_in": 100,     "tokens_out": 1234, "tokens_cached": 1234567,"description": "z"},
    ]

    out = render_output(header, 0, 0, 0, agents)
    lines = out.split("\n")
    agent_lines = lines[4:]

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

    out = render_output(header, 0, 0, 0, agents)
    lines = out.split("\n")
    # header + table header + sum + main + 1 agent = 5
    agent_line = lines[4]

    assert agent_line.startswith("[ok]")
    # description portion ends with U+2026
    assert "…" in agent_line, f"ellipsis missing from line {agent_line!r}"
    # The description column runs from after the status tag prefix up
    # to (but not including) the _DESC_TOKEN_GAP separator. We exclude
    # both the prefix and the trailing cell-section by computing the
    # cell section size from _TOKEN_COLUMN_WIDTH and adding
    # _DESC_TOKEN_GAP length. Slicing this way avoids splitting on
    # _DESC_TOKEN_GAP, which would mis-split on a description
    # containing internal double-space runs.
    prefix = "[ok]" + _STATUS_GAP
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

    out = render_output(header, 50, 30, 10, agents)
    lines = out.split("\n")
    sum_line = lines[2]

    # sum in = 350, format_tokens(350) = "350"
    assert sum_line.startswith("sum:")
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

    out = render_output(header, 0, 50, 0, agents)
    lines = out.split("\n")
    sum_line = lines[2]

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

    out = render_output(header, 0, 0, 0, agents)
    lines = out.split("\n")
    agent_line = lines[4]

    assert agent_line.startswith("[run]")
    assert "Working on it" in agent_line
    # 2500 → round(2.5)=2 (banker's) → "2k"
    # 800 → "800"
    # 1500 → round(1.5)=2 → "2k"
    # Both in and cached collapse to "2k"; exactly two "2k" substrings
    # must appear in the line (one for in, one for cached). Verify the
    # count to guard against silent format regressions where only one
    # field collapses.
    assert agent_line.count("2k") == 2, (
        f"expected exactly 2 '2k' substrings (in=2500 + cached=1500), "
        f"got {agent_line.count('2k')} in line {agent_line!r}"
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

    out = render_output(header, 0, 0, 0, agents)
    lines = out.split("\n")
    agent_line = lines[4]

    assert agent_line.startswith("[kill]")
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

    out = render_output(header, 0, 0, 0, agents)
    lines = out.split("\n")
    agent_line = lines[4]

    assert agent_line.startswith("[kill]")
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

    out = render_output(header, 0, 0, 0, agents)
    lines = out.split("\n")
    agent_line = lines[4]

    assert agent_line.startswith("[?]")


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

    out = render_output(header, 0, 0, 0, agents)
    lines = out.split("\n")
    # header + table header + sum + main + 1 agent = 5
    agent_line = lines[4]

    assert agent_line.startswith("[run]")
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
    """2000 input_tokens renders as '2k', not '2000'. format_tokens is
    applied BEFORE :>W, not after."""
    header = "Session: x"
    agents = [
        {"status": "ok", "tokens_in": 2000, "tokens_out": 0, "tokens_cached": 0, "description": "big"},
    ]

    out = render_output(header, 0, 0, 0, agents)
    lines = out.split("\n")
    agent_line = lines[4]

    # The "in" cell of the agent line should contain "2k" (formatted) and
    # NOT contain the literal "2000" as a substring (which would mean
    # format_tokens wasn't applied before :>W).
    assert "2k" in agent_line, (
        f"expected formatted '2k' in agent line {agent_line!r}"
    )
    assert "2000" not in agent_line, (
        f"raw '2000' should have been formatted: {agent_line!r}"
    )


# ---------------------------------------------------------------------------
# table header row — three labels right-aligned under their columns
# ---------------------------------------------------------------------------

def test_table_header_row() -> None:
    """Second line (after header) contains the three labels in/out/cached,
    each right-aligned within its own column width. The widths match what
    sum/main/agent rows use, so the labels line up with the cells below."""
    header = "Session: x"
    agents = [
        {"status": "ok", "tokens_in": 50000, "tokens_out": 200, "tokens_cached": 700, "description": "a"},
    ]

    out = render_output(header, 1000, 0, 0, agents)
    lines = out.split("\n")
    table_header = lines[1]

    # all three labels present
    assert "in" in table_header
    assert "out" in table_header
    assert "cached" in table_header
    # not a sum/main line
    assert not table_header.startswith("sum:")
    assert not table_header.startswith("main:")

    # The table header has exactly the three labels separated by single
    # spaces, each right-aligned to the column width. We can verify by
    # reconstructing what the renderer would produce, using the
    # production _col_width helper (so the test tracks the formula
    # rather than recomputing it).
    in_width = _col_width([50000, 1000], "in")
    out_width = _col_width([200, 0], "out")
    cached_width = _col_width([700, 0], "cached")
    expected_table_header = (
        f"{'in':>{in_width}} {'out':>{out_width}} {'cached':>{cached_width}}"
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
    # 1000 → "1k" (round to nearest k)
    assert format_tokens(1000) == "1k"
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

    out = render_output(header, 0, 0, 0, agents)
    agent_line = out.split("\n")[4]

    assert agent_line.startswith("[?]"), (
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

    out = render_output(header, -10, 0, 0, agents)
    lines = out.split("\n")
    main_line = lines[3]  # main row
    agent_line = lines[4]

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
