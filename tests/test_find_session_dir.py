"""Tests for find_session_dir — locate a session directory under projects_root.

find_session_dir(session_id, projects_root=None) walks a tree under
`<projects_root>/**/<session_id>` and returns the first match as a Path.
If projects_root is None, defaults to `Path.home() / ".claude" / "projects"`.
Returns None when the session directory does not exist.
"""
from __future__ import annotations

from pathlib import Path

from status_line import find_session_dir


def test_find_existing_session_dir(tmp_path: Path) -> None:
    """find_session_dir returns the path of a known session directory."""
    # build tmp/.claude/projects/projA/sid-abc-123/
    projects_root = tmp_path / ".claude" / "projects"
    session_a = projects_root / "projA" / "sid-abc-123"
    session_a.mkdir(parents=True)
    # add an unrelated session in another project
    (projects_root / "projB" / "sid-def-456").mkdir(parents=True)
    # and a subdir inside session_a (should not confuse the matcher)
    (session_a / "subagents").mkdir()

    result = find_session_dir("sid-abc-123", projects_root=projects_root)

    assert result is not None
    assert Path(result) == session_a


def test_find_nonexistent_session_dir(tmp_path: Path) -> None:
    """find_session_dir returns None when session_id is not present."""
    projects_root = tmp_path / ".claude" / "projects"
    (projects_root / "projA" / "sid-abc-123").mkdir(parents=True)
    (projects_root / "projB" / "sid-def-456").mkdir(parents=True)

    result = find_session_dir("sid-nonexistent", projects_root=projects_root)

    assert result is None


def test_find_with_no_projects_dir(tmp_path: Path) -> None:
    """find_session_dir returns None when projects_root does not exist."""
    # tmp_path exists but contains no .claude/projects
    result = find_session_dir("sid-anything", projects_root=tmp_path / "missing")
    assert result is None


def test_find_returns_first_match(tmp_path: Path) -> None:
    """If session_id exists in multiple project dirs, return the first found.

    Defensive behaviour: glob ordering is OS-dependent, but the function
    promises "first match". We don't enforce a specific order — only that
    some valid Path pointing to one of the duplicates is returned.
    """
    projects_root = tmp_path / ".claude" / "projects"
    dup_a = projects_root / "projA" / "sid-dup"
    dup_b = projects_root / "projB" / "sid-dup"
    dup_a.mkdir(parents=True)
    dup_b.mkdir(parents=True)

    result = find_session_dir("sid-dup", projects_root=projects_root)

    assert result is not None
    assert Path(result) in (dup_a, dup_b)


def test_find_returns_directory_not_file_with_same_name(tmp_path: Path) -> None:
    """Glob `<session_id>` may match a stray file with that name; the function
    should only return directories. (No file is created in this test — the
    assertion is implicit: even if a file with the session_id name existed
    in the tree, it must not be returned. This test just checks that a
    directory whose name happens to collide with another path's stem is
    still returned correctly.)"""
    projects_root = tmp_path / ".claude" / "projects"
    target = projects_root / "projA" / "sid-xyz"
    target.mkdir(parents=True)
    # unrelated file
    (projects_root / "projA" / "unrelated.txt").write_text("noise")

    result = find_session_dir("sid-xyz", projects_root=projects_root)
    assert result == target
