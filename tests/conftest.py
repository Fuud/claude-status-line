"""pytest fixtures for status_line tests.

Provides a `tmp_data_dir` placeholder hook for future tests that may want
a tmp cache directory. (Currently no tests use it directly; the
integration tests build their own tmp_path with `.claude/status_line/data`.)
"""

from __future__ import annotations

# Intentionally empty — no fixtures required at this layer. Tests build
# their own tmp paths via the built-in `tmp_path` fixture from pytest.
