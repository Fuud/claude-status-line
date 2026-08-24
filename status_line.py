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


# ---------------------------------------------------------------------------
# compute_main_cum
# ---------------------------------------------------------------------------

# [decision] We scan the jsonl twice on a cache miss: once from the end to
# locate the last assistant uuid (cheap — typically a few hundred KB tail),
# then once from the start to sum usage and extract tool_use positions. A
# single forward pass would be cleaner, but tail-scanning the uuid first lets
# us short-circuit on cache hits before doing the expensive forward scan.

def _read_last_assistant_event(jsonl_path: Path) -> dict | None:
    """Return the LAST assistant event dict in jsonl_path, or None if none.

    Reads file in reverse line-by-line. Returns the full event dict (not just
    uuid) so callers can extract usage. Returns None if the file does not
    exist, is unreadable, or contains no assistant events.
    """
    if not jsonl_path.exists():
        return None
    try:
        with jsonl_path.open("rb") as f:
            lines = f.readlines()
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            line = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            return event
    return None


def _read_last_event(jsonl_path: Path) -> dict | None:
    """Return the LAST event dict in jsonl_path (any type), or None if the
    file is missing/unreadable/empty. Used by detect_status, which classifies
    status from the very last line regardless of event type (e.g. user
    events with '[Request interrupted by user]' should yield 'stop')."""
    if not jsonl_path.exists():
        return None
    try:
        with jsonl_path.open("rb") as f:
            lines = f.readlines()
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            line = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            return event
    return None


def _read_last_assistant_uuid(jsonl_path: Path) -> str:
    """Return the uuid of the LAST assistant event in jsonl_path, or "" if
    none. Reads the file in reverse line-by-line for efficiency."""
    if not jsonl_path.exists():
        return ""
    try:
        # readlines() is fine for our jsonl sizes (sub-MB per session).
        with jsonl_path.open("rb") as f:
            lines = f.readlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            line = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # partial line — race with subagent writing; skip
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            return event.get("uuid", "") or ""
    return ""


def _scan_main_jsonl(jsonl_path: Path) -> tuple[int, int, int, int, dict[str, int], str]:
    """Forward-scan a main jsonl summing token usage and extracting tool_use
    positions.

    Returns (cum_in, cum_out, cum_cache_create, cum_cache_read,
             tool_use_positions, last_uuid). last_uuid is "" if no assistant
    event was found.
    """
    cum_in = cum_out = cum_cache_create = cum_cache_read = 0
    tool_use_positions: dict[str, int] = {}
    last_uuid = ""

    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        for index, raw_line in enumerate(f):
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # partial line — race condition with subagent writing; skip
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") != "assistant":
                continue
            # record uuid for this assistant event
            uuid = event.get("uuid")
            if isinstance(uuid, str) and uuid:
                last_uuid = uuid
            # usage
            msg = event.get("message") or {}
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if isinstance(usage, dict):
                cum_in += int(usage.get("input_tokens", 0) or 0)
                cum_out += int(usage.get("output_tokens", 0) or 0)
                cum_cache_create += int(usage.get("cache_creation_input_tokens", 0) or 0)
                cum_cache_read += int(usage.get("cache_read_input_tokens", 0) or 0)
            # tool_use positions
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    block_id = block.get("id")
                    if isinstance(block_id, str) and block_id:
                        # keep first occurrence only
                        if block_id not in tool_use_positions:
                            tool_use_positions[block_id] = index

    return (
        cum_in,
        cum_out,
        cum_cache_create,
        cum_cache_read,
        tool_use_positions,
        last_uuid,
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write `payload` to `path` atomically via a sibling .tmp + os.replace."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def compute_main_cum(jsonl_path: Path, cache_path: Path) -> dict:
    """Compute cumulative tokens from a main session jsonl, with cache by
    last_uuid.

    On cache hit (cache.last_uuid == current jsonl tail uuid), returns the
    cached dict without re-scanning. On cache miss (uuid changed, cache
    missing, or cache malformed), re-scans the jsonl forward, sums
    input/output/cache_creation/cache_read across all assistant events,
    collects tool_use id → event-index positions, and atomically writes the
    result to `cache_path`.

    Returns a dict with keys:
        cum_in, cum_out, cum_cache_create, cum_cache_read, total, last_uuid,
        tool_use_positions

    If `jsonl_path` does not exist, returns a zero-valued result without
    writing the cache.
    """
    empty_result = {
        "cum_in": 0,
        "cum_out": 0,
        "cum_cache_create": 0,
        "cum_cache_read": 0,
        "total": 0,
        "last_uuid": "",
        "tool_use_positions": {},
    }

    if not jsonl_path.exists():
        return dict(empty_result)

    # 1. Try to load cache.
    cache: dict | None = None
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
            else:
                # defensive: cache is JSON but not a dict → delete and recompute
                try:
                    cache_path.unlink()
                except OSError:
                    pass
        except json.JSONDecodeError:
            # broken cache → delete and recompute
            try:
                cache_path.unlink()
            except OSError:
                pass

    # 2. Read the last assistant uuid from the jsonl tail.
    last_uuid = _read_last_assistant_uuid(jsonl_path)

    # 3. Cache hit?
    if (
        cache is not None
        and last_uuid
        and cache.get("last_uuid") == last_uuid
    ):
        return cache

    # 4. Cache miss → forward scan.
    cum_in, cum_out, cum_cache_create, cum_cache_read, positions, scanned_uuid = (
        _scan_main_jsonl(jsonl_path)
    )
    # Prefer the scanned uuid over the tail-read one — they should match, but
    # the forward scan is authoritative.
    if scanned_uuid:
        last_uuid = scanned_uuid

    result = {
        "cum_in": cum_in,
        "cum_out": cum_out,
        "cum_cache_create": cum_cache_create,
        "cum_cache_read": cum_cache_read,
        "total": cum_in + cum_out + cum_cache_create + cum_cache_read,
        "last_uuid": last_uuid,
        "tool_use_positions": positions,
    }

    # 5. Atomic write to cache (skip if jsonl missing — handled above).
    _atomic_write_json(cache_path, result)

    return result


# ---------------------------------------------------------------------------
# compute_agent_snapshot
# ---------------------------------------------------------------------------

# [deviation] Plan spec says "agent с 0 assistant event-ов → status='err'".
# Pure detect_status would return "run" for that case (last event is user,
# no error/stop/ok match). I add an explicit override here: if there are
# zero assistant events in the jsonl at all, status is forced to "err"
# (or "stop" if meta.stoppedByUser=true — consistent with the rest of the
# logic). This is a deliberate behavior difference from detect_status, called
# out in the deviation log.

# Description truncation uses U+2026 HORIZONTAL ELLIPSIS ("…"), not three
# ASCII dots. Per the rendering rules in the plan's Technical Details section.
_DESCRIPTION_MAX_LEN = 40
_DESCRIPTION_ELLIPSIS = "…"  # …


def _truncate_description(s: str) -> str:
    """Truncate `s` to at most 40 chars; if longer, take first 39 + U+2026."""
    if len(s) > _DESCRIPTION_MAX_LEN:
        return s[: _DESCRIPTION_MAX_LEN - 1] + _DESCRIPTION_ELLIPSIS
    return s


def _load_meta_dict(meta_path: Path) -> dict:
    """Read meta_path as JSON; return {} on any failure (missing file,
    OSError, malformed JSON, non-dict payload)."""
    try:
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if isinstance(loaded, dict):
        return loaded
    return {}


def compute_agent_snapshot(
    jsonl_path: Path, meta_path: Path, cache_entry: dict | None
) -> dict:
    """Return snapshot dict for a single subagent.

    Returns a dict with keys:
        agentId      — jsonl filename without `.jsonl` extension
        status       — one of {"ok","err","stop","run"} (see detect_status)
        tokens       — sum of input+output+cache_creation+cache_read from
                       the last assistant event's `usage`; None if there is
                       no assistant event or status is "run" (mid-flow).
        description  — meta.description, truncated to 40 chars with "…".
                       Falls back to meta.agentType, then "unknown".
        toolUseId    — meta.toolUseId (string; "" if missing)
        last_uuid    — uuid of the last assistant event, or None
        mtime_jsonl  — st_mtime of jsonl_path, or 0.0 if missing
        mtime_meta   — st_mtime of meta_path, or 0.0 if missing

    Cache hit: if `cache_entry` is provided AND its last_uuid AND
    mtime_jsonl match the current jsonl state, the cache_entry is returned
    unchanged. This function does NOT write to any cache file — the caller
    (orchestrator) owns cache persistence.

    [deviation] When the jsonl contains zero assistant events at all, status
    is forced to "err" (or "stop" if meta.stoppedByUser=true) regardless of
    what detect_status would return. See module-level note above.
    """
    # 1. mtime_jsonl (0.0 if missing).
    try:
        mtime_jsonl = jsonl_path.stat().st_mtime
    except OSError:
        mtime_jsonl = 0.0

    # 2. Last assistant event (None if no assistant events) — drives tokens
    # and last_uuid. We also need the LAST event of any type for status
    # detection (e.g. agent_stopped_user has a user "[Request interrupted]"
    # event after the last assistant).
    last_event = _read_last_assistant_event(jsonl_path)
    last_jsonl_event = _read_last_event(jsonl_path)

    # 3. Load meta ({} on any failure).
    meta = _load_meta_dict(meta_path)

    # 4. Cache hit check.
    last_uuid_for_compare: str | None = (
        last_event.get("uuid") if last_event else None
    )
    if cache_entry is not None and isinstance(cache_entry, dict):
        if (
            cache_entry.get("last_uuid") == last_uuid_for_compare
            and cache_entry.get("mtime_jsonl") == mtime_jsonl
        ):
            return cache_entry

    # 5. Compute fields.
    agent_id = jsonl_path.stem

    # status — apply "0 assistant events → err" override. Otherwise pass
    # the LAST event of any type to detect_status (e.g. user interrupted).
    if last_event is None:
        # No assistant events at all in the jsonl.
        if meta.get("stoppedByUser") is True:
            status = "stop"
        else:
            status = "err"
    else:
        # detect_status inspects the very last jsonl line for type=user with
        # '[Request interrupted by user]'. If the jsonl has no last event
        # (degenerate empty file mid-write), fall back to the last assistant.
        detect_input = last_jsonl_event if last_jsonl_event is not None else last_event
        status = detect_status(detect_input, meta)

    # tokens — None if no assistant event or status is "run"; otherwise sum
    # input + output + cache_creation + cache_read from last_event.message.usage.
    if last_event is None or status == "run":
        tokens: int | None = None
    else:
        msg = last_event.get("message") or {}
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if isinstance(usage, dict):
            tokens = (
                int(usage.get("input_tokens", 0) or 0)
                + int(usage.get("output_tokens", 0) or 0)
                + int(usage.get("cache_creation_input_tokens", 0) or 0)
                + int(usage.get("cache_read_input_tokens", 0) or 0)
            )
        else:
            tokens = None

    # description — meta.description, fallback to meta.agentType, then
    # "unknown". Truncate to 40 chars with U+2026 if longer.
    description = meta.get("description") or ""
    if not description:
        description = meta.get("agentType") or "unknown"
    description = _truncate_description(description)

    tool_use_id = meta.get("toolUseId") or ""

    last_uuid = last_uuid_for_compare

    try:
        mtime_meta = meta_path.stat().st_mtime
    except OSError:
        mtime_meta = 0.0

    return {
        "agentId": agent_id,
        "status": status,
        "tokens": tokens,
        "description": description,
        "toolUseId": tool_use_id,
        "last_uuid": last_uuid,
        "mtime_jsonl": mtime_jsonl,
        "mtime_meta": mtime_meta,
    }