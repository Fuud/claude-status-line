"""Tests for render_output — assemble the multi-line status line string.

render_output(header, main_total, agents) returns a string built as:
    header
    sum: <sum_total>     # only if len(agents) > 0
    main: <main_total>
    for each agent:
        "[<status>]  <description>  <tokens>"   # tokens omitted when None

Status icons: "[ok]", "[run]", "[err]", "[stop]" (ASCII tags, 5 chars
including brackets). Description column padded to a fixed width so token
counts right-align cleanly. Description >40 chars is truncated with U+2026.
"""
from __future__ import annotations

from status_line import render_output


# ---------------------------------------------------------------------------
# single ok agent
# ---------------------------------------------------------------------------

def test_single_ok_agent() -> None:
    """1 agent [ok] with tokens → 4 lines: header, sum, main, agent line."""
    header = "Session: abc | Branch: master | Model: X | User: u"
    main_total = 1000
    agents = [
        {"status": "ok", "tokens": 500, "description": "Task 1: foo"},
    ]

    out = render_output(header, main_total, agents)
    lines = out.split("\n")

    assert len(lines) == 4
    assert lines[0] == header
    # sum = main_total (1000) + agent tokens (500) = 1500 → "2k"
    # (1000 + 500 = 1500, which formats as 1500 → round(1.5) → "2k")
    assert lines[1] == "sum: 2k"
    assert lines[2] == "main: 1k"
    # agent line
    assert lines[3].startswith("[ok]")
    assert "Task 1: foo" in lines[3]
    assert "500" in lines[3]


# ---------------------------------------------------------------------------
# zero agents — no sum line
# ---------------------------------------------------------------------------

def test_zero_agents_no_sum_line() -> None:
    """0 agents → only header + main line (no sum line)."""
    header = "Session: abc"
    main_total = 42

    out = render_output(header, main_total, [])
    lines = out.split("\n")

    assert len(lines) == 2
    assert lines[0] == header
    assert lines[1] == "main: 42"
    # no "sum:" line at all
    assert "sum:" not in out


# ---------------------------------------------------------------------------
# 38 agents → 41 lines (1 header + 1 sum + 1 main + 38 agents)
# ---------------------------------------------------------------------------

def test_38_agents_produce_41_lines() -> None:
    """38 agents → 41 lines: 1 header + 1 sum + 1 main + 38 agent lines."""
    header = "Session: big | Branch: m | Model: X | User: u"
    agents = [
        {
            "status": "ok",
            "tokens": (i + 1) * 100,
            "description": f"Agent {i}",
        }
        for i in range(38)
    ]

    out = render_output(header, 5000, agents)
    lines = out.split("\n")

    assert len(lines) == 41
    assert lines[0] == header
    assert lines[1].startswith("sum:")
    assert lines[2].startswith("main:")
    # remaining 38 lines all start with [ok] (or other status tag)
    for line in lines[3:]:
        assert line.startswith("[")


# ---------------------------------------------------------------------------
# token alignment — right-aligned to a fixed width column
# ---------------------------------------------------------------------------

def test_token_alignment_right_aligned() -> None:
    """Tokens are right-aligned to the same column width regardless of
    digit count. Varied token counts: 10, 50000 (50k), 1234567 (1.2M)."""
    header = "Session: x"
    agents = [
        {"status": "ok", "tokens": 10,       "description": "taskA"},
        {"status": "ok", "tokens": 50000,    "description": "taskB"},
        {"status": "ok", "tokens": 1234567,  "description": "taskC"},
    ]

    out = render_output(header, 0, agents)
    lines = out.split("\n")
    # skip header, sum, main
    agent_lines = lines[3:]

    # each agent line has the token string right-aligned to a fixed column.
    # Tokens of different widths will have different START positions but
    # must have identical END positions (the right edge of the line).
    token_strings = ["10", "50k", "1.2M"]
    end_positions = []
    for line, expected_token in zip(agent_lines, token_strings):
        # the line contains the formatted token somewhere
        assert expected_token in line, (
            f"expected token {expected_token!r} in line {line!r}"
        )
        # the END of the token = right edge of the formatted column.
        end = line.rfind(expected_token) + len(expected_token)
        end_positions.append(end)

    # all token END positions identical → right-aligned
    assert len(set(end_positions)) == 1, (
        f"tokens not right-aligned: ends={end_positions} from "
        f"lines {agent_lines}"
    )


# ---------------------------------------------------------------------------
# long description truncated with U+2026
# ---------------------------------------------------------------------------

def test_long_description_truncated() -> None:
    """Description with 60 chars → truncated to 40 chars with U+2026 ellipsis
    as the last char."""
    header = "Session: x"
    long_desc = "A" * 60  # 60 chars
    assert len(long_desc) == 60
    agents = [
        {"status": "ok", "tokens": 100, "description": long_desc},
    ]

    out = render_output(header, 0, agents)
    lines = out.split("\n")
    agent_line = lines[3]

    # the description portion of the line (after the status tag and spaces)
    # should end with U+2026
    assert "…" in agent_line, f"ellipsis missing from line {agent_line!r}"
    # the rendered description width ≤ 40 (status tag + 2 spaces + desc)
    # extract the description portion: after "[ok]  "
    prefix = "[ok]"
    assert agent_line.startswith(prefix)
    desc_part = agent_line[len(prefix):].lstrip()
    # take the description up to the first whitespace before the tokens
    # the line format is "<status>  <description>  <tokens>"
    # the description is everything before the LAST whitespace gap before tokens
    # simpler: split on the 2+ space gap
    parts = agent_line.split("  ")
    # parts: [status, description, tokens]
    desc = parts[1] if len(parts) >= 3 else ""
    # description width ≤ 40
    assert len(desc) <= 40, f"description width {len(desc)} > 40: {desc!r}"
    # last char is U+2026
    assert desc.endswith("…"), f"description not ellipsised: {desc!r}"


# ---------------------------------------------------------------------------
# agent with no tokens — line has no token column
# ---------------------------------------------------------------------------

def test_agent_with_no_tokens() -> None:
    """Agent with tokens=None → line shows only [status] description (no
    token column)."""
    header = "Session: x"
    agents = [
        {"status": "err", "tokens": None, "description": "Crash: foo"},
    ]

    out = render_output(header, 0, agents)
    lines = out.split("\n")
    agent_line = lines[3]

    # starts with [err]
    assert agent_line.startswith("[err]")
    # contains the description
    assert "Crash: foo" in agent_line
    # and no token column — no digit-only tokens like "0" or "1" should
    # appear at the end. We just check there's no whitespace-then-digits
    # pattern at the end.
    # Specifically: the line should end with the description.
    # Find the description position
    idx = agent_line.find("Crash: foo")
    after = agent_line[idx + len("Crash: foo"):]
    # after the description there should only be whitespace (no token digits)
    assert after.strip() == "", (
        f"unexpected content after description: {after!r}"
    )


# ---------------------------------------------------------------------------
# sum calculation
# ---------------------------------------------------------------------------

def test_sum_calculation() -> None:
    """sum = main_total + sum(agent.tokens for agent in agents if tokens
    is not None). Agents with tokens=None don't contribute."""
    header = "Session: x"
    agents = [
        {"status": "ok",  "tokens": 100,  "description": "a"},
        {"status": "err", "tokens": None, "description": "b"},  # excluded
        {"status": "ok",  "tokens": 200,  "description": "c"},
    ]

    out = render_output(header, 50, agents)
    lines = out.split("\n")
    sum_line = lines[1]

    # 50 + 100 + 200 = 350 → "350"
    assert sum_line == "sum: 350"
