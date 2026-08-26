"""Tests for session-dir lookup under projects_root.

find_session_dirs(session_id, projects_root=None) walks a tree under
`<projects_root>/**/<session_id>` and returns ALL matching directories as
a list of Paths, in glob (OS-dependent) order. The same session id can
exist in more than one encoded project dir (main checkout + worktree
copy), and callers merge agents across all of them.

find_session_dir(session_id, projects_root=None) is a thin wrapper over
find_session_dirs that returns the first match as a Path (or None) — the
historical single-match contract.

If projects_root is None, both default to `Path.home() / ".claude" / "projects"`.
find_session_dirs returns [] when nothing matches; find_session_dir returns None.

Tests can drive `projects_root=None` resolution via monkeypatching
`Path.home()` (preferred for new tests — keeps the public surface clean).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from status_line import find_session_dir, find_session_dirs


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


def test_find_existing_session_dir_via_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as above but resolves Path.home() via monkeypatch — exercises
    the default code path (projects_root=None) without exposing a
    test-only parameter on the public API."""
    projects_root = tmp_path / ".claude" / "projects"
    target = projects_root / "projA" / "sid-monkey-123"
    target.mkdir(parents=True)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = find_session_dir("sid-monkey-123")
    assert result == target


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


def test_find_with_empty_session_id() -> None:
    """Empty session_id → return None without touching the filesystem."""
    result = find_session_dir("", projects_root=Path("/nonexistent"))
    assert result is None


def test_find_returns_first_match(tmp_path: Path) -> None:
    """If session_id exists in multiple project dirs, return the first match.

    glob() in pathlib yields matches in arbitrary but OS-dependent order;
    we pin the expected "first" by controlling file-system order on
    ext4/NTFS (creation order on Linux, name order on Windows). Either
    dup_a or dup_b is a valid result — but the function must pick one
    deterministically (no random shuffling across calls).
    """
    projects_root = tmp_path / ".claude" / "projects"
    dup_a = projects_root / "projA" / "sid-dup"
    dup_b = projects_root / "projB" / "sid-dup"
    dup_a.mkdir(parents=True)
    dup_b.mkdir(parents=True)

    result = find_session_dir("sid-dup", projects_root=projects_root)

    assert result is not None
    assert Path(result) in (dup_a, dup_b)

    # Determinism: calling twice on the same tree returns the same Path.
    result2 = find_session_dir("sid-dup", projects_root=projects_root)
    assert result2 == result, (
        f"non-deterministic: first call returned {result}, second {result2}"
    )


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


def test_find_skips_file_with_matching_name(tmp_path: Path) -> None:
    """Stray file with the session_id name in another project must not be
    returned; the function filters to is_dir() and falls through to the
    directory match."""
    projects_root = tmp_path / ".claude" / "projects"
    # File with the target name in projA
    (projects_root / "projA").mkdir(parents=True)
    (projects_root / "projA" / "sid-collide").write_text("not a dir")
    # Real directory with the same name in projB
    target = projects_root / "projB" / "sid-collide"
    target.mkdir(parents=True)

    result = find_session_dir("sid-collide", projects_root=projects_root)
    assert result == target


# ---------------------------------------------------------------------------
# find_session_dirs — all matches
# ---------------------------------------------------------------------------


def test_find_session_dirs_returns_all_matches(tmp_path: Path) -> None:
    """Two same-named session dirs in different projects → both in result."""
    projects_root = tmp_path / ".claude" / "projects"
    dup_a = projects_root / "projA" / "sid-dup"
    dup_b = projects_root / "projB" / "sid-dup"
    dup_a.mkdir(parents=True)
    dup_b.mkdir(parents=True)

    result = find_session_dirs("sid-dup", projects_root=projects_root)

    assert sorted(result) == sorted([dup_a, dup_b])


def test_find_session_dirs_single_match(tmp_path: Path) -> None:
    """One matching directory → list containing exactly that one path."""
    projects_root = tmp_path / ".claude" / "projects"
    target = projects_root / "projA" / "sid-only"
    target.mkdir(parents=True)
    # unrelated session in another project must not leak in
    (projects_root / "projB" / "sid-other").mkdir(parents=True)

    result = find_session_dirs("sid-only", projects_root=projects_root)

    assert result == [target]


def test_find_session_dirs_glob_order_is_stable(tmp_path: Path) -> None:
    """Result order is the glob order — deterministic across calls (though
    OS-dependent across platforms, so we only pin stability, not content)."""
    projects_root = tmp_path / ".claude" / "projects"
    for proj in ("projA", "projB", "projC"):
        (projects_root / proj / "sid-order").mkdir(parents=True)

    result = find_session_dirs("sid-order", projects_root=projects_root)
    result2 = find_session_dirs("sid-order", projects_root=projects_root)

    assert len(result) == 3
    assert result == result2


def test_find_session_dirs_nonexistent_sid(tmp_path: Path) -> None:
    """Nonexistent session_id → empty list."""
    projects_root = tmp_path / ".claude" / "projects"
    (projects_root / "projA" / "sid-abc").mkdir(parents=True)

    result = find_session_dirs("sid-nonexistent", projects_root=projects_root)

    assert result == []


def test_find_session_dirs_empty_sid() -> None:
    """Empty session_id → empty list without touching the filesystem."""
    result = find_session_dirs("", projects_root=Path("/nonexistent"))
    assert result == []


def test_find_session_dirs_missing_projects_root(tmp_path: Path) -> None:
    """Nonexistent projects_root → empty list."""
    result = find_session_dirs(
        "sid-anything", projects_root=tmp_path / "missing"
    )
    assert result == []


def test_find_session_dirs_ignores_files_with_matching_name(
    tmp_path: Path,
) -> None:
    """Stray files (not dirs) named like the session_id are filtered out;
    only real directories are returned."""
    projects_root = tmp_path / ".claude" / "projects"
    (projects_root / "projA").mkdir(parents=True)
    # file masquerading as the session dir in projA
    (projects_root / "projA" / "sid-fake").write_text("not a dir")
    # real dir in projB
    target = projects_root / "projB" / "sid-fake"
    target.mkdir(parents=True)

    result = find_session_dirs("sid-fake", projects_root=projects_root)

    assert result == [target]


def test_find_session_dir_is_first_of_dirs(tmp_path: Path) -> None:
    """find_session_dir is a thin wrapper: first element of
    find_session_dirs, or None when the list is empty."""
    projects_root = tmp_path / ".claude" / "projects"
    (projects_root / "projA" / "sid-wrap").mkdir(parents=True)
    (projects_root / "projB" / "sid-wrap").mkdir(parents=True)

    dirs = find_session_dirs("sid-wrap", projects_root=projects_root)
    assert len(dirs) == 2
    assert find_session_dir("sid-wrap", projects_root=projects_root) == dirs[0]

    # no match → [] and None stay consistent too
    assert find_session_dirs("sid-missing", projects_root=projects_root) == []
    assert find_session_dir("sid-missing", projects_root=projects_root) is None
