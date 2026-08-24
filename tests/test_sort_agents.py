"""Tests for sort_agents — order subagent snapshots by toolUseId position.

sort_agents(agents, tool_use_positions) returns a NEW list of agents sorted
by `(tool_use_positions.get(toolUseId, +inf), mtime_meta)`. Python's
`sorted()` is stable, so agents with identical keys preserve input order.
"""
from __future__ import annotations

from status_line import sort_agents


def test_sort_agents_by_tool_use_position() -> None:
    """Three agents with toolUseId present in positions → sorted by position
    ascending."""
    agents = [
        {"toolUseId": "Agent_103", "mtime_meta": 100, "agentId": "agent1"},
        {"toolUseId": "Agent_110", "mtime_meta": 200, "agentId": "agent2"},
        {"toolUseId": "Agent_107", "mtime_meta": 150, "agentId": "agent3"},
    ]
    positions = {"Agent_103": 5, "Agent_107": 10, "Agent_110": 20}

    result = sort_agents(agents, positions)

    # expected order: agent1 (pos=5), agent3 (pos=10), agent2 (pos=20)
    assert [a["agentId"] for a in result] == ["agent1", "agent3", "agent2"]


def test_sort_agent_without_tool_use_id_goes_last() -> None:
    """Agent with toolUseId missing from positions sorts LAST (sentinel +inf)."""
    agents = [
        {"toolUseId": "Agent_103", "mtime_meta": 100, "agentId": "agent1"},
        {"toolUseId": "call_unknown", "mtime_meta": 300, "agentId": "agent4"},
        {"toolUseId": "Agent_110", "mtime_meta": 200, "agentId": "agent2"},
    ]
    positions = {"Agent_103": 5, "Agent_110": 20}

    result = sort_agents(agents, positions)

    # agent1 (5), agent2 (20), agent4 (inf) regardless of its mtime_meta
    assert [a["agentId"] for a in result] == ["agent1", "agent2", "agent4"]


def test_sort_stable_for_same_key() -> None:
    """Stable sort: agents with identical (position, mtime_meta) preserve
    input order. We force identical keys by giving two agents the same
    toolUseId (one with no mtime difference) — but toolUseId in dict maps
    uniquely; the more reliable construction is two agents both MISSING
    from positions (both get +inf) and with equal mtime_meta."""
    agents = [
        {"toolUseId": "call_a", "mtime_meta": 100, "agentId": "first"},
        {"toolUseId": "call_b", "mtime_meta": 100, "agentId": "second"},
        {"toolUseId": "call_c", "mtime_meta": 100, "agentId": "third"},
    ]
    # positions is empty → all three get +inf; mtime_meta are equal
    positions: dict = {}

    result = sort_agents(agents, positions)

    # all sorted equally → stable sort preserves original input order
    assert [a["agentId"] for a in result] == ["first", "second", "third"]


def test_sort_does_not_mutate_input() -> None:
    """sort_agents returns a NEW list — the input list is not mutated."""
    agents = [
        {"toolUseId": "Agent_110", "mtime_meta": 200, "agentId": "agent2"},
        {"toolUseId": "Agent_103", "mtime_meta": 100, "agentId": "agent1"},
    ]
    positions = {"Agent_103": 5, "Agent_110": 20}
    input_snapshot = list(agents)

    result = sort_agents(agents, positions)

    assert agents == input_snapshot  # original list is untouched
    assert result is not agents      # different object
    # but result has the agents in sorted order
    assert [a["agentId"] for a in result] == ["agent1", "agent2"]


def test_sort_empty_list() -> None:
    """sort_agents([]) returns []."""
    assert sort_agents([], {"Agent_103": 5}) == []
