"""Tests for _resolve_session_dirs — transcript_path-prioritized dir lookup.

_resolve_session_dirs(transcript_path, session_id, projects_root=None)
returns ALL glob matches of `find_session_dirs` (session dirs duplicated
across main checkout / worktree copies), but puts
`Path(transcript_path).parent / session_id` FIRST when that directory
exists — transcript_path is CC's authoritative statement of where the
session lives (same priority logic as _find_main_jsonl), and the first
entry wins agent dedup downstream. If glob already returned that dir, it
is moved to the front, never duplicated. Empty transcript_path, or one
whose sibling session dir does not exist, degrades to pure glob order.
"""
from __future__ import annotations

from pathlib import Path

from status_line import _resolve_session_dirs, find_session_dirs


SID = "sid-abc-123"


def _make_projects(tmp_path: Path) -> Path:
    projects_root = tmp_path / ".claude" / "projects"
    projects_root.mkdir(parents=True)
    return projects_root


def test_transcript_dir_first_when_among_glob_matches(tmp_path: Path) -> None:
    """Transcript session dir exists AND is one of the glob matches →
    it is first, and the rest follow in glob order — no duplicate."""
    projects_root = _make_projects(tmp_path)
    dup_a = projects_root / "projA" / SID
    dup_b = projects_root / "projB" / SID
    dup_a.mkdir(parents=True)
    dup_b.mkdir(parents=True)
    # transcript jsonl sits next to dup_b → dup_b is the authoritative dir
    transcript = dup_b.parent / f"{SID}.jsonl"
    transcript.write_text("{}")

    result = _resolve_session_dirs(str(transcript), SID, projects_root)

    glob_order = find_session_dirs(SID, projects_root)
    assert result[0] == dup_b
    assert len(result) == len(glob_order), "preferred dir must not be duplicated"
    assert sorted(result) == sorted(glob_order)


def test_transcript_dir_outside_projects_root_comes_first(tmp_path: Path) -> None:
    """Transcript dir exists but glob cannot see it (it lives outside
    projects_root) → it is still first, glob matches follow it."""
    projects_root = _make_projects(tmp_path)
    inside = projects_root / "projA" / SID
    inside.mkdir(parents=True)
    outside_area = tmp_path / "elsewhere"
    outside = outside_area / SID
    outside.mkdir(parents=True)
    transcript = outside_area / f"{SID}.jsonl"
    transcript.write_text("{}")

    result = _resolve_session_dirs(str(transcript), SID, projects_root)

    assert result[0] == outside
    assert result[1:] == [inside]


def test_empty_transcript_path_returns_pure_glob(tmp_path: Path) -> None:
    """Empty transcript_path → exactly find_session_dirs output (glob order)."""
    projects_root = _make_projects(tmp_path)
    for proj in ("projA", "projB"):
        (projects_root / proj / SID).mkdir(parents=True)

    result = _resolve_session_dirs("", SID, projects_root)

    assert result == find_session_dirs(SID, projects_root)


def test_nonexistent_transcript_path_returns_pure_glob(tmp_path: Path) -> None:
    """transcript_path points at a location that does not exist at all →
    fallback to pure glob order."""
    projects_root = _make_projects(tmp_path)
    (projects_root / "projA" / SID).mkdir(parents=True)
    (projects_root / "projB" / SID).mkdir(parents=True)
    transcript = tmp_path / "nowhere" / f"{SID}.jsonl"  # parent dir missing

    result = _resolve_session_dirs(str(transcript), SID, projects_root)

    assert result == find_session_dirs(SID, projects_root)


def test_missing_sibling_dir_returns_pure_glob(tmp_path: Path) -> None:
    """transcript_path parent exists, but `parent / session_id` is not a
    directory → no preferred dir, pure glob order."""
    projects_root = _make_projects(tmp_path)
    (projects_root / "projA" / SID).mkdir(parents=True)
    transcript = tmp_path / "elsewhere.jsonl"  # no `<sid>` sibling anywhere

    result = _resolve_session_dirs(str(transcript), SID, projects_root)

    assert result == find_session_dirs(SID, projects_root)


def test_transcript_dir_only_no_glob_matches(tmp_path: Path) -> None:
    """Transcript dir exists (outside the globbed tree) and glob found
    nothing → single-element list with the preferred dir."""
    projects_root = _make_projects(tmp_path)
    (projects_root / "projA" / "other-sid").mkdir(parents=True)
    outside_area = tmp_path / "elsewhere"
    outside = outside_area / SID
    outside.mkdir(parents=True)
    transcript = outside_area / f"{SID}.jsonl"

    result = _resolve_session_dirs(str(transcript), SID, projects_root)

    assert result == [outside]


def test_transcript_file_absent_but_dir_present_prefers_dir(tmp_path: Path) -> None:
    """Only `parent / session_id` existence matters, not the transcript
    file itself — a cleaned-up jsonl with a surviving session dir still
    prioritizes that dir (subagents may live there)."""
    projects_root = _make_projects(tmp_path)
    dup_a = projects_root / "projA" / SID
    dup_a.mkdir(parents=True)
    transcript = projects_root / "projB" / f"{SID}.jsonl"
    transcript.parent.mkdir(parents=True)  # jsonl file NOT created
    (projects_root / "projB" / SID).mkdir()

    result = _resolve_session_dirs(str(transcript), SID, projects_root)

    assert result[0] == projects_root / "projB" / SID
    assert result[1:] == [dup_a]


def test_empty_session_id_returns_empty_list(tmp_path: Path) -> None:
    """Empty session_id → [] even when transcript_path points at an
    existing directory (guard: `parent / ""` would resolve to parent)."""
    projects_root = _make_projects(tmp_path)
    (projects_root / "projA" / SID).mkdir(parents=True)
    transcript = projects_root / "projA" / f"{SID}.jsonl"

    result = _resolve_session_dirs(str(transcript), "", projects_root)

    assert result == []


def test_windows_backslash_transcript_path_keeps_dir_priority(tmp_path: Path) -> None:
    """Windows CC sends backslash-separated transcript paths; under cygwin
    python (the production hook interpreter) posixpath treats such a string
    as ONE opaque component, so Path(...).parent degenerates to "." and the
    transcript dir silently loses its priority to pure glob order. The
    backslashes must be normalized so the transcript dir still comes first,
    without duplicating it. (On Windows-native python this test is green
    either way — WindowsPath understands backslashes natively; the red
    demonstration is on posixpath, i.e. cygwin/WSL-style interpreters.)"""
    projects_root = _make_projects(tmp_path)
    dup_a = projects_root / "projA" / SID
    dup_b = projects_root / "projB" / SID
    dup_a.mkdir(parents=True)
    dup_b.mkdir(parents=True)
    transcript = dup_b.parent / f"{SID}.jsonl"
    transcript.write_text("{}")
    # simulate CC's Windows-style payload: every separator is a backslash
    windows_style = str(transcript).replace("/", "\\")

    result = _resolve_session_dirs(windows_style, SID, projects_root)

    glob_order = find_session_dirs(SID, projects_root)
    assert result[0] == dup_b
    assert len(result) == len(glob_order), "preferred dir must not be duplicated"
    assert sorted(result) == sorted(glob_order)
