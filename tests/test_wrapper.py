import subprocess
from pathlib import Path

SH_PATH = Path(__file__).parent.parent / "status_line.sh"


def test_wrapper_syntax():
    """bash -n checks syntax of the wrapper without executing."""
    result = subprocess.run(["bash", "-n", str(SH_PATH)], capture_output=True)
    assert result.returncode == 0, f"bash -n failed: {result.stderr.decode()}"


def test_wrapper_empty_object_stdin():
    """Wrapper end-to-end: empty-object stdin → header line, exit 0.

    The test feeds `b"{}"` (a parseable empty JSON object) — a
    representative "no fields" payload from the hook. The status line
    must still emit a header line (with empty session_id) and exit 0."""
    result = subprocess.run(
        ["bash", str(SH_PATH)], input=b"{}", capture_output=True, timeout=10
    )
    assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
    output = result.stdout.decode()
    assert "Session:" in output, f"no header in: {output!r}"


def test_wrapper_with_session_id():
    """Wrapper with a real-shape stdin → at least the header line."""
    import json
    stdin = json.dumps({
        "session_id": "test-session-123",
        "model": {"display_name": "test-model"},
        "context_window": {"used_percentage": 0, "total_input_tokens": 0},
    }).encode()
    result = subprocess.run(
        ["bash", str(SH_PATH)], input=stdin, capture_output=True, timeout=10
    )
    assert result.returncode == 0
    output = result.stdout.decode()
    assert "Session: test-session-123" in output
