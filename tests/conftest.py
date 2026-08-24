"""pytest fixtures for status_line tests.

Provides `tmp_data_dir` — a pytest fixture returning a tmp Path for the
status_line data/ cache directory. Tests use this to point
compute_main_cum / compute_agent_snapshot cache paths at an isolated
location without polluting the real `~/.claude/status_line/data/`.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path: pytest.TempPathFactory) -> "pytest.Path":
    """Return a tmp directory path to be used as a status_line `data/` cache.

    The directory is created (empty) by pytest's `tmp_path`. Tests are
    responsible for creating sub-paths inside it as needed (e.g.
    `tmp_data_dir / "main_<sid>.json"`).
    """
    return tmp_path
