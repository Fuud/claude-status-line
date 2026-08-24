"""Tests for _find_main_jsonl — resolve the main session jsonl path.

_find_main_jsonl(transcript_path, session_id, session_dir, projects_root=None)
resolves in priority order: payload transcript_path (existing file) →
sibling of a found session_dir → one-level glob
`<projects_root>/*/<session_id>.jsonl`. Returns None when session_id is
empty or nothing matches. `projects_root` defaults to
`Path.home() / ".claude" / "projects"`; tests inject it explicitly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from status_line import _find_main_jsonl


SID = "sid-abc-123"


def _make_projects(tmp_path: Path) -> Path:
    projects_root = tmp_path / ".claude" / "projects"
    projects_root.mkdir(parents=True)
    return projects_root


def test_transcript_path_wins(tmp_path: Path) -> None:
    """An existing transcript_path file is returned even when a sibling
    jsonl also exists — CC's own statement is the primary source."""
    projects_root = _make_projects(tmp_path)
    session_dir = projects_root / "projA" / SID
    session_dir.mkdir(parents=True)
    sibling = session_dir.parent / f"{SID}.jsonl"
    sibling.write_text("{}")
    via_payload = tmp_path / "elsewhere.jsonl"
    via_payload.write_text("{}")

    result = _find_main_jsonl(str(via_payload), SID, session_dir, projects_root)

    assert result == via_payload


def test_transcript_path_missing_file_falls_through(tmp_path: Path) -> None:
    """A transcript_path that doesn't exist on disk is skipped, not an
    error — fall through to the session_dir sibling."""
    projects_root = _make_projects(tmp_path)
    session_dir = projects_root / "projA" / SID
    session_dir.mkdir(parents=True)
    sibling = session_dir.parent / f"{SID}.jsonl"
    sibling.write_text("{}")

    result = _find_main_jsonl(
        str(tmp_path / "gone.jsonl"), SID, session_dir, projects_root
    )

    assert result == sibling


def test_sibling_used_without_transcript_path(tmp_path: Path) -> None:
    """No payload transcript_path → sibling of the found session_dir."""
    projects_root = _make_projects(tmp_path)
    session_dir = projects_root / "projA" / SID
    session_dir.mkdir(parents=True)
    sibling = session_dir.parent / f"{SID}.jsonl"
    sibling.write_text("{}")

    result = _find_main_jsonl("", SID, session_dir, projects_root)

    assert result == sibling


def test_glob_finds_dirless_session(tmp_path: Path) -> None:
    """The dirless case this helper exists for: no `<sid>/` directory and
    no transcript_path — the one-level glob still locates the jsonl."""
    projects_root = _make_projects(tmp_path)
    jsonl = projects_root / "projA" / f"{SID}.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text("{}")

    result = _find_main_jsonl("", SID, None, projects_root)

    assert result == jsonl


def test_glob_ignores_nested_jsonl(tmp_path: Path) -> None:
    """The glob is one level (`*/`), not recursive — a jsonl with the same
    name nested under a session dir must not match (documented deviation)."""
    projects_root = _make_projects(tmp_path)
    nested = projects_root / "projA" / "other-sid" / f"{SID}.jsonl"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}")

    result = _find_main_jsonl("", SID, None, projects_root)

    assert result is None


def test_missing_sibling_with_session_dir_falls_to_glob(tmp_path: Path) -> None:
    """session_dir exists but its sibling jsonl is gone (manual cleanup) →
    glob fallback still resolves another copy if present."""
    projects_root = _make_projects(tmp_path)
    (projects_root / "projA" / SID).mkdir(parents=True)
    jsonl = projects_root / "projB" / f"{SID}.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text("{}")

    result = _find_main_jsonl("", SID, projects_root / "projA" / SID, projects_root)

    assert result == jsonl


def test_nothing_found_returns_none(tmp_path: Path) -> None:
    """Unknown session id, no transcript_path → None (header-only degrade)."""
    projects_root = _make_projects(tmp_path)
    (projects_root / "projA").mkdir(parents=True)
    (projects_root / "projA" / "unrelated.jsonl").write_text("{}")

    assert _find_main_jsonl("", "no-such-sid", None, projects_root) is None


def test_empty_session_id_returns_none(tmp_path: Path) -> None:
    """Empty session_id short-circuits to None even with a valid
    transcript_path — nothing sane to resolve against."""
    projects_root = _make_projects(tmp_path)
    some_file = tmp_path / "x.jsonl"
    some_file.write_text("{}")

    assert _find_main_jsonl(str(some_file), "", None, projects_root) is None


def test_missing_projects_root_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """projects_root default (Path.home()/.claude/projects) absent → None,
    not a crash — same guard as find_session_dir."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = _find_main_jsonl("", SID, None)

    assert result is None


def test_default_projects_root_via_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """projects_root=None resolves via Path.home() — the production path."""
    projects_root = _make_projects(tmp_path)
    jsonl = projects_root / "projA" / f"{SID}.jsonl"
    jsonl.parent.mkdir(parents=True)
    jsonl.write_text("{}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = _find_main_jsonl("", SID, None)

    assert result == jsonl
