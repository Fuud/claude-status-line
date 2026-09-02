import json
import os
import subprocess
from pathlib import Path

import pytest

SH_PATH = Path(__file__).parent.parent / "status_line.sh"


def _bash() -> str:
    """A bash that can actually execute the wrapper.

    Under native Windows Python the first `bash` on PATH is the WSL
    relay (C:\\Windows\\System32\\bash.exe), which fails without a WSL
    distro installed. Probe CLAUDE_CODE_GIT_BASH_PATH (the real shell
    Claude Code uses), then the Cygwin default, then plain `bash`.
    """
    candidates = [
        os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", ""),
        r"C:\cygwin64\bin\bash.exe",
        "bash",
    ]
    for cand in candidates:
        if cand and subprocess.run(
            [cand, "-c", "exit 0"], capture_output=True
        ).returncode == 0:
            return cand
    pytest.skip("no working bash found on this machine")


def test_wrapper_syntax():
    """bash -n checks syntax of the wrapper without executing."""
    result = subprocess.run(
        [_bash(), "-n", str(SH_PATH)], capture_output=True
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr.decode()}"


def test_wrapper_empty_object_stdin():
    """Wrapper end-to-end: empty-object stdin → header line, exit 0.

    The test feeds `b"{}"` (a parseable empty JSON object) — a
    representative "no fields" payload from the hook. The status line
    must still emit a header line (with empty session_id) and exit 0."""
    result = subprocess.run(
        [_bash(), str(SH_PATH)], input=b"{}", capture_output=True, timeout=30
    )
    assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
    output = result.stdout.decode()
    assert "Session:" in output, f"no header in: {output!r}"


def test_wrapper_with_session_id():
    """Wrapper with a real-shape stdin → at least the header line."""
    stdin = json.dumps({
        "session_id": "test-session-123",
        "model": {"display_name": "test-model"},
        "context_window": {"used_percentage": 0, "total_input_tokens": 0},
    }).encode()
    result = subprocess.run(
        [_bash(), str(SH_PATH)], input=stdin, capture_output=True, timeout=30
    )
    assert result.returncode == 0
    output = result.stdout.decode()
    assert "Session: test-session-123" in output
