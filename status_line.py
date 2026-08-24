"""status_line.py — pure functions for Claude Code status line aggregation.

This module currently contains the pure (no I/O) functions:
- format_tokens(n)        — human-readable token counts
- detect_status(...)      — agent status from last jsonl event + meta
- parse_stdin(json_str)   — parse Claude Code hook stdin payload

Only stdlib is used. Later tasks add I/O, caching, and a main() entry point.

Module-level invariants:
- format_tokens handles non-negative ints.
- detect_status returns one of {"err", "stop", "ok", "run"}.
- parse_stdin never raises; it returns a dict with all keys present.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# format_tokens
# ---------------------------------------------------------------------------

def format_tokens(n: int) -> str:
    """Format a token count as a short human-readable string.

    Rules:
        n < 1_000           → "N"          (e.g. "850")
        1_000 <= n < 1e6    → "Nk"         (e.g. "78k")
        n >= 1_000_000      → "N.NM"       (e.g. "1.2M", 1 decimal)

    [decision] Round-half-to-even for k: 999500 → "1000k". Python's built-in
    round() uses banker's rounding, which still gives 1000 for 999.5, so this
    matches the test expectation.
    """
    if n < 0:
        # defensive: status line never shows negative; treat as 0
        n = 0
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        # round to nearest k
        k = round(n / 1_000)
        return f"{k}k"
    # millions branch — 1 decimal place
    m = round(n / 1_000_000, 1)
    # if rounding produced a value that rounds up to next integer (e.g. 9.96),
    # format as "10.0M" rather than collapsing — caller can adjust if needed
    return f"{m:.1f}M"


# ---------------------------------------------------------------------------
# detect_status
# ---------------------------------------------------------------------------

def _is_assistant_error(last_event: dict) -> bool:
    """True if last_event is an assistant event with an API error marker."""
    if last_event.get("type") != "assistant":
        return False
    msg = last_event.get("message", {}) or {}
    if msg.get("error"):
        return True
    if msg.get("isApiErrorMessage") is True:
        return True
    api_status = msg.get("apiErrorStatus")
    if isinstance(api_status, int) and api_status >= 400:
        return True
    return False


def _is_user_interrupted(last_event: dict) -> bool:
    """True if last_event is a user event with the 'Request interrupted'
    marker somewhere in its content (string or list-of-blocks form)."""
    if last_event.get("type") != "user":
        return False
    msg = last_event.get("message", {}) or {}
    content = msg.get("content")
    needle = "Request interrupted by user"
    if isinstance(content, str):
        return needle in content
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text") or block.get("content")
            if isinstance(text, str) and needle in text:
                return True
            # tool_result blocks — content may be nested list of dicts
            inner = block.get("content")
            if isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict):
                        t = sub.get("text") or sub.get("content")
                        if isinstance(t, str) and needle in t:
                            return True
    return False


def _is_assistant_end_turn(last_event: dict) -> bool:
    """True if last_event is an assistant event with stop_reason='end_turn'."""
    if last_event.get("type") != "assistant":
        return False
    msg = last_event.get("message", {}) or {}
    return msg.get("stop_reason") == "end_turn"


def detect_status(last_event: dict, meta: dict) -> str:
    """Classify an agent's status from its last jsonl event and meta dict.

    Priority (highest first):
        err   — last event is assistant AND has error/isApiErrorMessage/
                apiErrorStatus>=400
        stop  — meta.stoppedByUser=true OR last event is user with
                'Request interrupted by user' marker
        ok    — last event is assistant with stop_reason='end_turn'
                (no error already excluded above)
        run   — anything else (mid-flow, no assistant events)

    Returns one of {"err", "stop", "ok", "run"}.
    """
    meta = meta or {}

    if _is_assistant_error(last_event):
        return "err"

    if meta.get("stoppedByUser") is True:
        return "stop"

    if _is_user_interrupted(last_event):
        return "stop"

    if _is_assistant_end_turn(last_event):
        return "ok"

    return "run"


# ---------------------------------------------------------------------------
# parse_stdin
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "session_id": "",
    "prompt_id": "",
    "model": "",
    "branch": "",
    "ctx_k": 0,
    "used_pct": 0,
    "user": "n/a",
}


def _get_branch() -> str:
    """Return current git branch (empty string on any error or non-git cwd)."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def parse_stdin(json_str: str) -> dict:
    """Parse the JSON payload that Claude Code delivers via stdin to the
    status line hook, returning a dict with stable keys.

    Never raises. On invalid JSON, empty input, or missing fields, returns
    sensible defaults.

    The function shells out to `git branch --show-current` to fill the
    `branch` field — uses subprocess with a 2-second timeout and returns ""
    on any failure (not a git repo, git missing, timeout).
    """
    # defensive copy so we don't mutate the module default dict on error paths
    out = dict(_DEFAULTS)
    out["branch"] = _get_branch()

    if not json_str or not json_str.strip():
        return out

    try:
        payload = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return out

    if not isinstance(payload, dict):
        return out

    sid = payload.get("session_id", "")
    if isinstance(sid, str):
        out["session_id"] = sid

    pid = payload.get("prompt_id", "")
    if isinstance(pid, str):
        out["prompt_id"] = pid

    model = payload.get("model", {})
    if isinstance(model, dict):
        name = model.get("display_name", "")
        if isinstance(name, str):
            out["model"] = name

    cw = payload.get("context_window", {})
    if isinstance(cw, dict):
        total = cw.get("total_input_tokens", 0)
        if isinstance(total, (int, float)):
            out["ctx_k"] = int(total // 1_000) if total >= 0 else 0
        used = cw.get("used_percentage", 0)
        if isinstance(used, (int, float)):
            out["used_pct"] = int(round(used))

    # `user` is not derivable from the current payload (no host/uid field),
    # so we keep the default "n/a". Field is present so downstream renderers
    # don't have to check for it.
    return out