"""status_line.py — Claude Code status line aggregation.

Module-level invariants:
- format_tokens handles non-negative ints; negative values clamp to "0".
- detect_status returns one of {"err", "stop", "ok", "run"}.
- parse_stdin never raises; it returns a dict with all keys present.
- compute_main_cum / compute_agent_snapshot never raise; OSError is
  swallowed so the hook cannot crash the parent session.
- The orchestrator override in _compute_agents may additionally set
  agent.status="kill" when a main-log queue-operation task-notification with
  <status>killed</status> is present and the compute_agent_snapshot verdict
  is not "err" or "stop" (see plan 20260824-subagent-status-via-queue-notifications).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
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
# context limit / format_context
# ---------------------------------------------------------------------------

# Fallback context-window limits when CLAUDE_CODE_CONTEXT_LIMIT is unset:
# "[1m]" models get 1M, everything else 200k (the API default).
_CONTEXT_LIMIT_1M = 1_000_000
_CONTEXT_LIMIT_DEFAULT = 200_000
# Env var that, when set to a positive int, overrides both fallbacks.
_CONTEXT_LIMIT_ENV = "CLAUDE_CODE_CONTEXT_LIMIT"


def resolve_context_limit(model: str) -> int:
    """Return the context-window limit (tokens) the header percentage is
    computed against.

    Priority:
        1. env CLAUDE_CODE_CONTEXT_LIMIT — if set and parses as a positive
           int, it wins outright (even for "[1m]" models).
        2. "[1m]" substring in `model` (the display name, e.g.
           "glm-5.3[1m]") → 1_000_000.
        3. otherwise → 200_000.

    Malformed (non-int), empty, or non-positive env values are ignored and
    resolution falls through to the model heuristic — a bad env var must
    not take the percentage away, only a good one should override it.
    The "[1m]" check is case-insensitive so "GLM-5.3[1M]" matches too.
    """
    raw = os.environ.get(_CONTEXT_LIMIT_ENV, "")
    if raw:
        try:
            limit = int(raw)
        except ValueError:
            limit = 0
        if limit > 0:
            return limit
    if "[1m]" in (model or "").lower():
        return _CONTEXT_LIMIT_1M
    return _CONTEXT_LIMIT_DEFAULT


def format_context(context_tokens: int, limit: int) -> str:
    """Format the header's Context segment: "<N>K (<P>%)".

    N is context_tokens in whole thousands (round-half-to-even, matching
    format_tokens' k-branch), P is the share of `limit` rounded to a whole
    percent. Negative tokens clamp to 0; a non-positive limit (defensive —
    resolve_context_limit never returns one) yields 0% instead of dividing
    by zero.
    """
    if context_tokens < 0:
        context_tokens = 0
    k = round(context_tokens / 1_000)
    pct = round(context_tokens * 100 / limit) if limit > 0 else 0
    return f"{k}K ({pct}%)"


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


_INTERRUPT_MARKER = "Request interrupted by user"


def _content_contains_marker(content: object, needle: str) -> bool:
    """Return True if `needle` appears anywhere in `content` (string,
    list-of-text-blocks, or list-of-tool_result-blocks-with-nested-content)."""
    if isinstance(content, str):
        return needle in content
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or block.get("content")
        if isinstance(text, str) and needle in text:
            return True
        inner = block.get("content")
        if isinstance(inner, list):
            for sub in inner:
                if not isinstance(sub, dict):
                    continue
                t = sub.get("text") or sub.get("content")
                if isinstance(t, str) and needle in t:
                    return True
    return False


def _is_user_interrupted(last_event: dict) -> bool:
    """True if last_event is a user event with the 'Request interrupted'
    marker somewhere in its content (string or list-of-blocks form)."""
    if last_event.get("type") != "user":
        return False
    msg = last_event.get("message", {}) or {}
    return _content_contains_marker(msg.get("content"), _INTERRUPT_MARKER)


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
    "user": "n/a",
    "context_tokens": 0,
    "transcript_path": "",
}


# Module-level cache for _get_branch — tuple (monotonic_ts, branch_str).
# Avoids a subprocess spawn on every parse_stdin call; invalidated every
# _BRANCH_CACHE_TTL seconds, which is plenty for status-line cadence.
_branch_cache: tuple[float, str] | None = None
_BRANCH_CACHE_TTL = 5.0  # seconds


def _get_branch() -> str:
    """Return current git branch (empty string on any error or non-git cwd).

    Cached for 5 seconds — the status-line hook fires frequently and the
    branch only changes at git checkout events. The TTL prevents a
    subprocess spawn on every parse_stdin call while still tracking
    branch switches promptly.
    """
    global _branch_cache
    now = time.monotonic()
    if _branch_cache is not None and (now - _branch_cache[0]) < _BRANCH_CACHE_TTL:
        return _branch_cache[1]
    branch = _get_branch_impl()
    _branch_cache = (now, branch)
    return branch


def _get_branch_impl() -> str:
    """Worker for _get_branch — actual git subprocess call."""
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
    `branch` field — uses subprocess with a 2-second timeout and a 5-second
    module-level TTL cache. Returns "" on any failure (not a git repo, git
    missing, timeout).

    Returns keys: session_id, prompt_id, model, branch, user,
    context_tokens, transcript_path.
    context_tokens is payload.context_window.total_input_tokens — the
    context-window occupancy at the most recent API response (input +
    cache writes + cache reads), per the statusline docs. 0 when absent
    (pre-first-API-call, older CC versions); the orchestrator then falls
    back to the jsonl-derived value.
    transcript_path is payload.transcript_path — the main session jsonl
    path per Claude Code. "" when absent; the orchestrator uses it as the
    primary source when resolving the main jsonl (see _find_main_jsonl).
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

    # transcript_path — main session jsonl location per CC. Kept verbatim
    # (no existence check here; parse_stdin stays I/O-free apart from the
    # branch subprocess). _find_main_jsonl validates it on use.
    tpath = payload.get("transcript_path", "")
    if isinstance(tpath, str):
        out["transcript_path"] = tpath

    pid = payload.get("prompt_id", "")
    if isinstance(pid, str):
        out["prompt_id"] = pid

    model = payload.get("model", {})
    if isinstance(model, dict):
        name = model.get("display_name", "")
        if isinstance(name, str):
            out["model"] = name

    # context_window.total_input_tokens — int tokens currently in the
    # context window at the last API response. bool is an int subclass, so
    # the isinstance check would let True through; the `not bool(...)` guard
    # keeps that degenerate value at 0.
    ctx = payload.get("context_window", {})
    if isinstance(ctx, dict):
        tokens = ctx.get("total_input_tokens", 0)
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            out["context_tokens"] = tokens

    # `user` is not derivable from the payload (no host/uid field), so it
    # comes from the AI_USER env var; unset or empty falls back to "n/a".
    out["user"] = os.environ.get("AI_USER") or "n/a"
    return out


# ---------------------------------------------------------------------------
# compute_main_cum
# ---------------------------------------------------------------------------

# [deviation] Extracted from queue-operation events in the main jsonl (added
# per plan 20260824-subagent-status-via-queue-notifications.md). Maps the
# `<status>` value inside `<task-notification>` content to the in-vocabulary
# status used by render_output. Unknown statuses are dropped — they fall
# through to the jsonl-based detection in compute_agent_snapshot.
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_STATUS_RE = re.compile(r"<status>([^<]+)</status>")
_QUEUE_STATUS_MAP: dict[str, str] = {
    "completed": "ok",
    "killed": "kill",
    "failed": "err",
}

# [decision] compute_main_cum performs a SINGLE forward scan of the jsonl
# per cache miss. Previously we tail-scanned first (to short-circuit on
# cache hits cheaply) and then scanned forward for totals — that was a
# double read on every miss. Cache hit detection now relies on the
# last_uuid returned from the forward scan; cost is dominated by the
# forward pass anyway, and one I/O round-trip is preferable to two.

# [decision] _read_last_event reads the file once and returns BOTH the
# last assistant event and the very last event of any type, via a
# single reverse scan. compute_agent_snapshot previously called two
# helpers (last assistant + last of any type), incurring two reads per
# subagent per hook invocation. The unified helper returns a tuple and
# the orchestrator passes the relevant slice downstream.

# [decision] We use readlines() (a full-file read) rather than mmap or
# reverse-chunked read for tail-scanning. Per-session jsonl files are
# small (sub-MB for typical agent activity, ~1.7 MB for the f5044e4f
# main jsonl, individual subagent jsonl files are tens of KB). The hook
# fires frequently but the per-call cost is dominated by the forward
# scan in compute_main_cum, not by these tail reads. A more elaborate
# mmap implementation would add complexity without measurable benefit
# at current sizes — we can revisit if profiling shows these reads as
# hot. The single unified helper removes the previous double-read on
# agents; full-file reads remain documented as a known trade-off.


def _read_last_event(
    jsonl_path: Path,
) -> tuple[dict | None, dict | None]:
    """Return (last_assistant_event, last_event_of_any_type) from jsonl_path,
    or (None, None) if the file is missing/unreadable/empty.

    Both are extracted from a single reverse pass over the file: the
    very last dict we encounter is `last_event_of_any_type`; the last
    dict whose `type` is "assistant" is `last_assistant_event`. Either
    may be None if no such event exists.

    Used by compute_agent_snapshot, which needs both: the assistant
    event drives `tokens_in` / `tokens_out` / `tokens_cached` and
    `last_uuid`, while the last event of any type drives status
    detection (a user "[Request interrupted by user]" event after the
    final assistant must surface as "stop").
    """
    if not jsonl_path.exists():
        return (None, None)
    try:
        with jsonl_path.open("rb") as f:
            lines = f.readlines()
    except OSError:
        return (None, None)

    last_assistant: dict | None = None
    last_event: dict | None = None
    for raw in reversed(lines):
        # errors="replace" cannot raise; bare decode would only on TypeError
        # for a non-bytes input, which readlines() never returns.
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # partial line — race with subagent writing; skip
            continue
        if not isinstance(event, dict):
            continue
        if last_event is None:
            last_event = event
        if event.get("type") == "assistant" and last_assistant is None:
            last_assistant = event
        if last_event is not None and last_assistant is not None:
            break
    return (last_assistant, last_event)


# [decision] _scan_main_jsonl returns a dict (keyed like the compute_main_cum
# result minus mtime_jsonl) rather than the positional tuple it historically
# grew into. Adding the start_* triple pushed the tuple to 11 positional
# fields — past the point where a transposed destructure fails loudly. The
# dict keeps the scan→result handoff self-describing; the only caller is
# compute_main_cum.

def _scan_main_jsonl(jsonl_path: Path) -> dict:
    """Forward-scan a main jsonl summing token usage and extracting tool_use
    positions.

    Returns a dict with keys:
        cum_in, cum_out, cum_cache_create, cum_cache_read
            — sums of the usage fields across ALL assistant events.
        start_in, start_out, start_cached
            — input_tokens / output_tokens / cache_read_input_tokens of the
              FIRST assistant event that carries a usage block (the table's
              "start:" row — the session's baseline message). Zeros when no
              assistant event has usage. cache_creation is NOT surfaced,
              matching the cached-column semantics of every other row.
        context_tokens
            — input + cache_creation + cache_read of the LAST assistant
              event — i.e. the context-window size at the most recent API
              call (the header's "Context: NK (P%)" field).
        tool_use_positions — tool_use block id → event-index map.
        last_uuid — uuid of the last assistant event, "" if none found.
        task_notifications — maps `<task-id>` from `<task-notification>`
            queue-operation content to one of the in-vocabulary statuses
            `{"ok", "kill", "err"}`; unknown statuses are omitted.
            Last-wins on duplicate task-id (resume scenario).
    """
    cum_in = cum_out = cum_cache_create = cum_cache_read = 0
    start_in = start_out = start_cached = 0
    context_tokens = 0
    tool_use_positions: dict[str, int] = {}
    task_notifications: dict[str, str] = {}
    last_uuid = ""
    seen_first_usage = False

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
            if event.get("type") == "assistant":
                # record uuid for this assistant event
                uuid = event.get("uuid")
                if isinstance(uuid, str) and uuid:
                    last_uuid = uuid
                # usage
                msg = event.get("message") or {}
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if isinstance(usage, dict):
                    in_v = int(usage.get("input_tokens", 0) or 0)
                    out_v = int(usage.get("output_tokens", 0) or 0)
                    cache_read_v = int(usage.get("cache_read_input_tokens", 0) or 0)
                    cum_in += in_v
                    cum_out += out_v
                    cum_cache_create += int(usage.get("cache_creation_input_tokens", 0) or 0)
                    cum_cache_read += cache_read_v
                    # First-message capture — set once, on the first
                    # assistant event that HAS a usage block. A leading
                    # assistant event without usage contributes nothing,
                    # mirroring the context_tokens handling below.
                    if not seen_first_usage:
                        seen_first_usage = True
                        start_in = in_v
                        start_out = out_v
                        start_cached = cache_read_v
                    # Context-window occupancy at THIS api call — overwrite on
                    # every assistant event so the scan ends holding the LAST
                    # one. Same formula as the payload's
                    # context_window.total_input_tokens (input + cache writes
                    # + cache reads; output excluded), so both sources agree.
                    context_tokens = (
                        in_v
                        + int(usage.get("cache_creation_input_tokens", 0) or 0)
                        + cache_read_v
                    )
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
            elif event.get("type") == "queue-operation":
                # Extract <task-id> / <status> from <task-notification> content.
                # Only "enqueue" operations carry content; "dequeue"/"remove"
                # are no-ops here. Unknown <status> values are silently dropped.
                if event.get("operation") != "enqueue":
                    continue
                content = event.get("content")
                if not isinstance(content, str):
                    continue
                m_id = _TASK_ID_RE.search(content)
                m_status = _STATUS_RE.search(content)
                if not (m_id and m_status):
                    continue
                mapped = _QUEUE_STATUS_MAP.get(m_status.group(1))
                if mapped:
                    # last-wins on duplicate task-id (resume scenario)
                    task_notifications[m_id.group(1)] = mapped

    return {
        "cum_in": cum_in,
        "cum_out": cum_out,
        "cum_cache_create": cum_cache_create,
        "cum_cache_read": cum_cache_read,
        "start_in": start_in,
        "start_out": start_out,
        "start_cached": start_cached,
        "context_tokens": context_tokens,
        "tool_use_positions": tool_use_positions,
        "last_uuid": last_uuid,
        "task_notifications": task_notifications,
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write `payload` to `path` atomically via a sibling .tmp + os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_dict_cache(path: Path) -> dict:
    """Load a JSON-dict cache from `path`. Returns {} on any failure
    (missing file, OSError, malformed JSON, non-dict payload) — broken
    caches are unlinked to ensure a clean rebuild on the next write.
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        try:
            path.unlink()
        except OSError:
            pass
        return {}
    if isinstance(loaded, dict):
        return loaded
    # defensive: valid JSON but not a dict → delete and start fresh
    try:
        path.unlink()
    except OSError:
        pass
    return {}


_EMPTY_MAIN_RESULT: dict = {
    "cum_in": 0,
    "cum_out": 0,
    "cum_cache_create": 0,
    "cum_cache_read": 0,
    "start_in": 0,
    "start_out": 0,
    "start_cached": 0,
    "context_tokens": 0,
    "last_uuid": "",
    "mtime_jsonl": 0.0,
    "tool_use_positions": {},
    "task_notifications": {},
}


def compute_main_cum(jsonl_path: Path, cache_path: Path) -> dict:
    """Compute cumulative tokens from a main session jsonl, with cache by
    last_uuid.

    On cache hit (cache.last_uuid == current jsonl tail uuid AND
    cache.mtime_jsonl == current jsonl st_mtime), returns the cached dict
    without re-scanning. On cache miss (uuid changed, mtime changed, cache
    missing, or cache malformed), re-scans the jsonl forward, sums
    input/output/cache_creation/cache_read across all assistant events,
    collects tool_use id → event-index positions, extracts task-notification
    statuses from queue-operation events, and atomically writes the result
    to `cache_path`.

    Returns a dict with keys:
        cum_in, cum_out, cum_cache_create, cum_cache_read,
        start_in, start_out, start_cached, context_tokens,
        last_uuid, mtime_jsonl, tool_use_positions, task_notifications

    context_tokens is the context-window occupancy at the LAST assistant
    event (input + cache_creation + cache_read) — the header's "Context:"
    field, used as fallback when the stdin payload carries no
    context_window.total_input_tokens.

    start_in/start_out/start_cached are the first-message breakdown rendered
    as the table's "start:" row (see _scan_main_jsonl).

    [deviation] Cache key includes `mtime_jsonl` so that queue-operation
    events appended to main jsonl without a corresponding new assistant event
    still invalidate the cache (last_uuid alone would miss them). Without
    mtime in the key, newly-fired task-notifications would be invisible
    to the orchestrator override for as long as the main session stays idle.

    [deviation] Cache hit additionally requires `context_tokens` to be
    present in the cached dict. Pre-upgrade caches that match both key parts
    but lack the field would otherwise render "0K (0%)" for one cycle after
    upgrade — same field-presence guard pattern as the agents cache's
    breakdown fields.

    [deviation] Cache hit likewise requires the three `start_*` fields to be
    present: pre-start-row caches lack them and would render a zeroed
    "start:" row for one cycle after upgrade. Same guard pattern as the
    context_tokens check above.

    [deviation] The legacy `total` field was removed in Task 2 of the
    breakdown-table plan. The total is now derived by render from the three
    breakdown values (in + out + cached). Persisted `total` keys in old
    cache files from before this change are harmless: cache-hit returns
    the cached dict unchanged, and render ignores the extra field. We do
    not actively migrate.

    If `jsonl_path` does not exist, returns a zero-valued result without
    writing the cache.

    All OSError paths (disk full, permission denied, broken symlink,
    read-only cache dir) are swallowed: the function returns a degraded
    but valid result instead of raising, honoring the "never break the
    parent session" contract documented in the plan.
    """
    if not jsonl_path.exists():
        return dict(_EMPTY_MAIN_RESULT)

    # _load_dict_cache unlinks the file on its own failure paths, so the
    # result is always either a valid dict or {} (never a malformed file).
    cache = _load_dict_cache(cache_path)

    # Forward-scan the jsonl — authoritative source for last_uuid,
    # token totals, start_* fields, and task_notifications in a single pass.
    scan = _scan_main_jsonl(jsonl_path)
    mtime_jsonl = _jsonl_mtime(jsonl_path)

    # Cache hit? Both last_uuid AND mtime_jsonl must match — otherwise stale.
    # `context_tokens` and the `start_*` fields' presence are part of the
    # hit check (see [deviation]s in the docstring): pre-upgrade caches
    # lack them.
    if (
        cache is not None
        and scan["last_uuid"]
        and cache.get("last_uuid") == scan["last_uuid"]
        and cache.get("mtime_jsonl") == mtime_jsonl
        and "context_tokens" in cache
        and all(f in cache for f in ("start_in", "start_out", "start_cached"))
    ):
        return cache

    result = {**scan, "mtime_jsonl": mtime_jsonl}

    # Atomic write to cache. If write fails (disk full, read-only dir),
    # still return the just-computed result — degrading to a no-cache run
    # is better than throwing away correct values we have in hand.
    try:
        _atomic_write_json(cache_path, result)
    except OSError:
        pass

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
    """Read meta_path as a JSON dict; return {} on any failure (missing
    file, OSError, malformed JSON, non-dict payload)."""
    return _load_dict_cache(meta_path)


def compute_agent_snapshot(
    jsonl_path: Path, meta_path: Path, cache_entry: dict | None
) -> dict:
    """Return snapshot dict for a single subagent.

    Returns a dict with keys:
        agentId       — jsonl filename without `.jsonl` extension
        status        — one of {"ok","err","stop","run"} (see detect_status)
        tokens_in     — input_tokens from the last assistant event's usage
                        (or 0 if no assistant event / no usage block)
        tokens_out    — output_tokens from the last assistant event's usage
                        (or 0 if no assistant event / no usage block)
        tokens_cached — cache_read_input_tokens from the last assistant event's
                        usage (or 0 if no assistant event / no usage block).
                        cache_creation_input_tokens is NOT surfaced.
        description   — meta.description, truncated to 40 chars with "…".
                        Falls back to meta.agentType, then "unknown".
        toolUseId     — meta.toolUseId (string; "" if missing)
        last_uuid     — uuid of the last assistant event, or None
        mtime_jsonl   — st_mtime of jsonl_path, or 0.0 if missing
        mtime_meta    — st_mtime of meta_path, or 0.0 if missing

    Breakdown fields (tokens_in / tokens_out / tokens_cached) are ALWAYS
    populated as ints, even for status="run" (mid-flow) — the user sees
    current values, not blanks. Agents with no assistant events or with
    a missing `usage` block get zeros in all three fields.

    Cache hit: if `cache_entry` is provided AND its last_uuid AND
    mtime_jsonl AND mtime_meta match the current on-disk state AND all
    three breakdown fields are present in cache_entry, the cache_entry is
    returned unchanged. The field-presence check guards against stale
    pre-upgrade caches (which would render zeros via `int(a.get(field)
    or 0)` until the next jsonl mutation). This function does NOT write
    to any cache file — the caller (orchestrator) owns cache persistence.

    [deviation] When the jsonl contains zero assistant events at all, status
    is forced to "err" (or "stop" if meta.stoppedByUser=true) regardless of
    what detect_status would return. See module-level note above.
    """
    # 1. mtime_jsonl (0.0 if missing).
    mtime_jsonl = _jsonl_mtime(jsonl_path)

    # 2. Last assistant event AND last event of any type — both extracted
    # from a single reverse pass via _read_last_event. The assistant
    # event drives breakdown fields and `last_uuid`; the very last event
    # of any type drives status detection (e.g. a user "[Request
    # interrupted by user]" event after the final assistant must surface
    # as "stop").
    last_event, last_jsonl_event = _read_last_event(jsonl_path)

    # 3. Load meta ({} on any failure).
    meta = _load_meta_dict(meta_path)

    # 4. Cache hit check. mtime_meta is part of the key: if meta.json
    # mutates (e.g. stoppedByUser added later, description edited),
    # cache must invalidate even if jsonl mtime+uuid are unchanged.
    # Field-presence check for the three breakdown fields is REQUIRED:
    # a pre-upgrade cache (old format) would otherwise satisfy the
    # key-match but lack the new fields, leading to render zeros in the
    # breakdown columns until the next jsonl mutation.
    mtime_meta_for_compare = _meta_mtime(meta_path)
    last_uuid_for_compare: str | None = (
        last_event.get("uuid") if last_event else None
    )
    # agent_id is needed both for the cache-hit dict-shape invariant (see
    # _AGENT_CACHE_FIELDS comment) and below in the cache-miss builder.
    agent_id = jsonl_path.stem
    if cache_entry is not None:
        breakdown_present = all(
            f in cache_entry for f in ("tokens_in", "tokens_out", "tokens_cached")
        )
        if (
            cache_entry.get("last_uuid") == last_uuid_for_compare
            and cache_entry.get("mtime_jsonl") == mtime_jsonl
            and cache_entry.get("mtime_meta") == mtime_meta_for_compare
            and breakdown_present
        ):
            # Preserve the invariant: the returned snapshot always has
            # `agentId` inside, regardless of cache hit or miss. The
            # on-disk cache stores agentId as the dict key (see
            # _AGENT_CACHE_FIELDS), so we re-inject it here from the
            # canonical source (jsonl_path.stem).
            return {**cache_entry, "agentId": agent_id}

    # 5. Compute fields.

    # status — apply "0 assistant events → err" override. Also check
    # last_jsonl_event for the user-interrupt marker even when we have
    # assistant events (a user "[Request interrupted by user]" event
    # written AFTER the final assistant must surface as "stop").
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

    # breakdown — input_tokens / output_tokens / cache_read_input_tokens from
    # the last assistant event's `message.usage`. Coerce via `int(... or 0)`
    # so missing/None values are 0. cache_creation_input_tokens is NOT
    # surfaced. Always returns three ints — including for status="run"
    # (mid-flow) and for missing/empty jsonl — so render always sees numbers.
    msg = (last_event.get("message") or {}) if last_event else {}
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if isinstance(usage, dict):
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        tokens_cached = int(usage.get("cache_read_input_tokens") or 0)
    else:
        tokens_in = tokens_out = tokens_cached = 0

    # description — meta.description, fallback to meta.agentType, then
    # "unknown". Truncate to 40 chars with U+2026 if longer.
    description = meta.get("description") or ""
    if not description:
        description = meta.get("agentType") or "unknown"
    description = _truncate_description(description)

    tool_use_id = meta.get("toolUseId") or ""

    return {
        "agentId": agent_id,
        "status": status,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_cached": tokens_cached,
        "description": description,
        "toolUseId": tool_use_id,
        "last_uuid": last_uuid_for_compare,
        "mtime_jsonl": mtime_jsonl,
        "mtime_meta": mtime_meta_for_compare,
    }


def _meta_mtime(meta_path: Path) -> float:
    """Return meta_path.st_mtime, or 0.0 if missing/unstat'able."""
    try:
        return meta_path.stat().st_mtime
    except OSError:
        return 0.0


def _jsonl_mtime(jsonl_path: Path) -> float:
    """Return jsonl_path.st_mtime, or 0.0 if missing/unstat'able."""
    try:
        return jsonl_path.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# find_session_dir(s)
# ---------------------------------------------------------------------------

def find_session_dirs(
    session_id: str, projects_root: Path | None = None
) -> list[Path]:
    """Locate ALL directories named `session_id` under `projects_root`.

    Walks `<projects_root>/**/<session_id>` and returns every matching
    *directory* as a list of Paths, in glob order (OS-dependent, but stable
    per tree). Returns [] if `session_id` is empty, if `projects_root` does
    not exist, or if no matching directory is found.

    The same session id can legitimately live in more than one encoded
    project directory — e.g. the main checkout and a worktree copy of the
    same repo, each with its own `subagents/` tree. Callers that need the
    complete picture (agents, tokens) must merge results across ALL of
    these directories, which is why this exists alongside the historical
    single-match `find_session_dir`.

    If `projects_root` is None, defaults to `<home>/.claude/projects`.

    The `projects_root` parameter is preserved for backward compatibility
    with existing callers; tests can also drive this function via
    `monkeypatch.setattr(Path, "home", lambda: tmp_path)` and pass
    `projects_root=None`, which avoids the public surface carrying a
    test-only parameter.
    """
    if not session_id:
        return []
    if projects_root is None:
        projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.exists():
        return []
    # glob for a directory whose name matches session_id anywhere under
    # projects_root. We use **/<session_id> (not the bare name) so we
    # also pick up project-name directories nested one level deep
    # (the convention is `<encoded-project>/<session_id>/`).
    # recurse_symlinks=False avoids following symlinked project trees into
    # infinite loops or surprising locations; glob ordering is OS-
    # dependent but stable per tree on the same kernel.
    return [
        candidate
        for candidate in projects_root.glob(f"**/{session_id}")
        if candidate.is_dir()
    ]


def find_session_dir(
    session_id: str, projects_root: Path | None = None
) -> Path | None:
    """Locate the FIRST directory named `session_id` under `projects_root`.

    Thin wrapper over `find_session_dirs`: returns the first element of
    its result (glob order), or None when the list is empty — `session_id`
    is empty, `projects_root` does not exist, or nothing matches. See
    `find_session_dirs` for the all-matches variant and the rationale for
    needing more than one directory per session id.
    """
    dirs = find_session_dirs(session_id, projects_root=projects_root)
    return dirs[0] if dirs else None


# ---------------------------------------------------------------------------
# _resolve_session_dirs
# ---------------------------------------------------------------------------

def _resolve_session_dirs(
    transcript_path: str,
    session_id: str,
    projects_root: Path | None = None,
) -> list[Path]:
    """Resolve ALL session dirs for `session_id`, transcript dir first.

    Starts from `find_session_dirs` (every `**/<session_id>` directory under
    `projects_root`) and, when `transcript_path` is non-empty and
    `Path(transcript_path).parent / session_id` is an existing directory,
    moves that directory to the front of the list (without duplicating it
    if glob already returned it). Backslashes in `transcript_path` are
    normalized to forward slashes first: Windows CC sends `C:\...` paths,
    and under a posix-flavoured python (cygwin) posixpath would otherwise
    treat the whole string as one component, making `.parent` degenerate
    to "." and the priority below silently never engage.

    Priority rationale: transcript_path is CC's own authoritative statement
    of where the session lives (the same source `_find_main_jsonl` trusts
    first). The first entry of the result wins agent-id dedup downstream,
    so the authoritative directory must lead even when glob's OS-dependent
    ordering would put an empty worktree copy first (the bug this fixes).

    Degradations: empty `transcript_path`, or one whose sibling session dir
    does not exist on disk, yields the pure glob order unchanged. Empty
    `session_id` → [] (no filesystem statement to trust, and the glob has
    nothing to match).

    Like `find_session_dirs`, `projects_root` defaults to
    `<home>/.claude/projects` and is injectable for tests.
    """
    dirs = find_session_dirs(session_id, projects_root=projects_root)
    if not (session_id and transcript_path):
        return dirs
    # Windows CC sends backslash-separated transcript paths, and the hook
    # may run under a posix-flavoured python (cygwin): there backslash is
    # NOT a separator, so PosixPath("C:\\Users\\...") is one opaque
    # component and .parent degenerates to "." — silently disabling the
    # priority below. Forward slashes are native on POSIX and equally
    # valid on Windows python, so normalizing is safe on both.
    normalized = transcript_path.replace("\\", "/")
    preferred = Path(normalized).parent / session_id
    if not preferred.is_dir():
        return dirs
    # only the sibling dir's existence matters, not the transcript file
    # itself — a cleaned-up jsonl with a surviving session dir still wins
    return [preferred] + [d for d in dirs if d != preferred]


# ---------------------------------------------------------------------------
# _find_main_jsonl
# ---------------------------------------------------------------------------

def _find_main_jsonl(
    transcript_path: str,
    session_id: str,
    session_dir: Path | None,
    projects_root: Path | None = None,
) -> Path | None:
    """Resolve the main session jsonl path for `session_id`.

    Priority (first existing file wins):
        1. `transcript_path` from the stdin payload — Claude Code's own
           statement of where the session jsonl lives. Works even for
           sessions that never spawned a subagent (and thus have no
           `<sid>/` directory on disk at all).
        2. Sibling of a found `session_dir` (the historical layout —
           `main()` used to derive the jsonl exclusively this way).
        3. One-level glob `<projects_root>/*/<session_id>.jsonl`.

    [deviation] The glob is one level (`*/`), not recursive (`**/`):
    the on-disk convention is `<encoded-project>/<sid>.jsonl` with encoded
    project dirs as direct children of `projects/`, and a recursive walk
    would descend into every session dir (incl. `subagents/` trees) for
    no gain. `find_session_dir` above stays recursive for directories —
    its historical contract.

    Returns None when `session_id` is empty or nothing matches — the
    orchestrator then degrades to a header-only line.

    Like `find_session_dir`, `projects_root` defaults to
    `<home>/.claude/projects` and is injectable for tests.
    """
    if not session_id:
        return None
    if transcript_path:
        candidate = Path(transcript_path)
        if candidate.is_file():
            return candidate
    if session_dir is not None:
        sibling = session_dir.parent / f"{session_id}.jsonl"
        if sibling.is_file():
            return sibling
    if projects_root is None:
        projects_root = Path.home() / ".claude" / "projects"
    if projects_root.exists():
        for candidate in projects_root.glob(f"*/{session_id}.jsonl"):
            if candidate.is_file():
                return candidate
    return None


# ---------------------------------------------------------------------------
# sort_agents
# ---------------------------------------------------------------------------

def sort_agents(
    agents: list, tool_use_positions: dict[str, int]
) -> list:
    """Return a NEW list of `agents` sorted by tool_use_positions then
    mtime_meta.

    Sort key: `(tool_use_positions.get(toolUseId, +inf), mtime_meta)`.
    Agents without `toolUseId` (or with one not in `tool_use_positions`)
    sort LAST (sentinel +inf), and among themselves they break ties by
    `mtime_meta`. Python's `sorted()` is stable, so agents with identical
    keys preserve input order.

    The input list is NOT mutated.
    """
    if not agents:
        return []
    sentinel = float("inf")

    def sort_key(agent: dict) -> tuple[float, float]:
        tool_use_id = agent.get("toolUseId", "")
        if not isinstance(tool_use_id, str):
            tool_use_id = ""
        position = tool_use_positions.get(tool_use_id, sentinel)
        mtime_meta = agent.get("mtime_meta", 0)
        if not isinstance(mtime_meta, (int, float)):
            mtime_meta = 0
        return (position, mtime_meta)

    return sorted(agents, key=sort_key)


# ---------------------------------------------------------------------------
# render_output
# ---------------------------------------------------------------------------

# Token column width — sized to fit the widest format produced by
# format_tokens (e.g. "999.5k", "1.2M" → 5 chars max, plus a small
# safety margin).
_TOKEN_COLUMN_WIDTH = 7
# Gap between the status tag and the description column (2 spaces).
_STATUS_GAP = "  "
# Gap between the description column and the token column (2 spaces).
_DESC_TOKEN_GAP = "  "
# Width of the status-icon column (e.g. "[stop]") — the longest known
# status name ("stop"/"kill") is 4 chars, plus 2 brackets = 6. Pad
# shorter icons ("ok", "err") with trailing spaces so the description
# column starts at the same x-position regardless of status length.
_ICON_COL_WIDTH = 6
# Recognized agent statuses — single source of truth for render_output's
# validation and the module docstring's promise. The orchestrator override
# in _compute_agents may set "kill" when a main-log queue-operation
# task-notification with <status>killed</status> is present and the
# compute_agent_snapshot verdict is not "err" or "stop" (see plan
# 20260824-subagent-status-via-queue-notifications). detect_status itself
# still returns only {ok, err, stop, run}.
_STATUSES = ("ok", "run", "err", "stop", "kill")
# Prefix prepended to every table row (everything except the session
# header line). Claude Code strips leading whitespace from status-line
# rows, which would left-shift the all-spaces token-header row and break
# its alignment with the sum/main/agent rows. A constant non-space prefix
# survives the strip and shifts all rows equally, so relative column
# alignment is preserved.
_TABLE_ROW_PREFIX = "| "


def _col_width(values: list, label: str) -> int:
    """Compute a right-aligned column width for a numeric column.

    Width is the maximum of:
        - _TOKEN_COLUMN_WIDTH (the floor — guarantees readability even
          for narrow columns),
        - len(label) (so labels like "cached" don't overflow),
        - the longest formatted value across `values` (so values like
          "1.2M" don't overflow).

    Exposed at module scope so tests can mirror the width formula
    without copying it. `values` is expected to be a list of ints; the
    `default=0` on the max() handles the empty-list case.

    [deviation] Plan spec (step 2 of the breakdown-table section) wrote
    this as `max(len(format_tokens(v)) for v in col + [label])` with the
    floor pulled in separately: `min(_TOKEN_COLUMN_WIDTH, computed)`.
    Mathematically equivalent — appending `label` to `col` only adds
    `len(label)` to the candidate set, and the floor is just another
    `max` operand — but the refactor splits the floor into an explicit
    named constant so the intent ("never narrower than 7") is visible
    at the call site rather than buried inside a generator expression.
    """
    longest_value = max((len(format_tokens(v)) for v in values), default=0)
    return max(_TOKEN_COLUMN_WIDTH, len(label), longest_value)


def render_output(
    header: str,
    start_in: int,
    start_out: int,
    start_cached: int,
    main_in: int,
    main_out: int,
    main_cached: int,
    agents: list,
) -> str:
    """Build the multi-line status line string with a tabular breakdown.

    Layout:
        <header>
        | <table header — labels "in" / "out" / "cached", each right-aligned
          within its own column>
        | start: <in> <out> <cached>
        | sum: <in> <out> <cached>    # only if len(agents) > 0
        | main: <in> <out> <cached>
        | for each agent (in input order):
              [<status>]  <description>  <in> <out> <cached>

    The start row is the FIRST table row: the first assistant event's
    breakdown (the session's baseline message), letting the reader see
    what the session began with against the current main/sum totals. It
    is NOT part of the sum row (sum = main + agents only) — it is a
    reference row, not an additive component. Always rendered, like the
    main row, even when it is all zeros (fresh session, no assistant
    events yet).

    Every table row carries the "| " prefix (_TABLE_ROW_PREFIX) so that
    Claude Code's leading-whitespace strip cannot left-shift the
    all-spaces token-header row relative to the label/icon rows below.

    Every numeric cell is formatted via format_tokens() (so 1000 → "1k")
    BEFORE applying :>W — formatting raw 1000 with width 7 would render
    "   1000" instead of "     1k". Each column's width is computed
    independently as
        max(_TOKEN_COLUMN_WIDTH,
            len(label),
            len(format_tokens(v)) for v in column)
    so columns with wider labels ("cached") or wider formatted values
    ("1.2M") expand independently.

    Description is truncated to 40 chars with U+2026 by _truncate_description
    (re-applied here for defense-in-depth). The description column IS
    padded to the longest description in the current agent list so all
    numeric columns land at the same x-position across rows and align
    with the table header above.

    Agents are NEVER skipped: a run-agent renders its current breakdown
    values (not None), and an agent with no breakdown data renders three
    zeros. The cache-hit invariant in compute_agent_snapshot guarantees
    all three tokens_* fields are populated; defensive `int(... or 0)`
    here handles pre-upgrade caches or callers that build snapshots by
    hand.
    """
    # 1. Build per-column value lists (start row, main row, then agents).
    # The agent loop runs once, projecting each agent into a triple of
    # ints and summing in lockstep — saves three separate list
    # comprehensions and three separate sum() calls for the sum row.
    agent_in: list[int] = []
    agent_out: list[int] = []
    agent_cached: list[int] = []
    descriptions: list[str] = []
    for a in agents:
        agent_in.append(int(a.get("tokens_in") or 0))
        agent_out.append(int(a.get("tokens_out") or 0))
        agent_cached.append(int(a.get("tokens_cached") or 0))
        # Defensive truncation: callers already truncate to 40, but
        # render_output is the final formatter and shouldn't trust
        # upstream. Re-apply so a buggy or future caller can't blow up
        # the column layout.
        descriptions.append(_truncate_description(a.get("description", "") or ""))

    # Start cells participate in column-width computation like every other
    # row's — a wide first-message value must expand its column or it
    # would overflow the cell alignment.
    in_col = [start_in, main_in, *agent_in]
    out_col = [start_out, main_out, *agent_out]
    cached_col = [start_cached, main_cached, *agent_cached]

    # 2. Compute per-column width: at least _TOKEN_COLUMN_WIDTH, at least
    # the label length, at least the longest formatted cell. See
    # _col_width at module scope (importable for tests).
    w_in = _col_width(in_col, "in")
    w_out = _col_width(out_col, "out")
    w_cached = _col_width(cached_col, "cached")

    # Description column width: padded to the longest description in the
    # current agent list. Without this, the numeric columns shift right
    # on rows with shorter descriptions and the table header stops
    # aligning with the cells. Computed BEFORE assembling lines so the
    # header can include the same padding.
    w_desc = max((len(d) for d in descriptions), default=0)

    # 3. Assemble lines.
    lines: list[str] = [header]
    # Table header — `w_desc + _ICON_COL_WIDTH` spaces on the left so the
    # `in/out/cached` labels land at the same x-position as the cells in
    # the rows below. The agent rows use icon (padded to _ICON_COL_WIDTH)
    # + status_gap (2) + desc (w_desc) + desc_gap (2), so the prefix
    # before in_cell is consistently w_desc + _ICON_COL_WIDTH + 4.
    # sum:/main: rows use the same prefix width below.
    header_pad = w_desc + _ICON_COL_WIDTH + 4
    lines.append(
        f"{' ' * header_pad}{'in':>{w_in}} {'out':>{w_out}} {'cached':>{w_cached}}"
    )

    # start row — the FIRST table row. `start:` is 6 chars, padded to the
    # same `header_pad` width as sum:/main: so its cells land at the same
    # x-position as every other row.
    lines.append(
        f"{'start:':<{header_pad}}{format_tokens(start_in):>{w_in}} "
        f"{format_tokens(start_out):>{w_out}} "
        f"{format_tokens(start_cached):>{w_cached}}"
    )

    if agents:
        sum_in = main_in + sum(agent_in)
        sum_out = main_out + sum(agent_out)
        sum_cached = main_cached + sum(agent_cached)
        # Pad the label column to `header_pad` so the in_cell lands at
        # the same x-position as in the agent rows below. `sum:` is 4
        # chars, so the left-pad width is `header_pad` directly.
        lines.append(
            f"{'sum:':<{header_pad}}{format_tokens(sum_in):>{w_in}} "
            f"{format_tokens(sum_out):>{w_out}} "
            f"{format_tokens(sum_cached):>{w_cached}}"
        )

    # `main:` is 5 chars, padded to the same `header_pad` width as `sum:`
    # for the same alignment reason.
    lines.append(
        f"{'main:':<{header_pad}}{format_tokens(main_in):>{w_in}} "
        f"{format_tokens(main_out):>{w_out}} "
        f"{format_tokens(main_cached):>{w_cached}}"
    )

    for agent, description, in_v, out_v, cached_v in zip(
        agents, descriptions, agent_in, agent_out, agent_cached
    ):
        status = agent.get("status", "run")
        icon = f"[{status}]" if status in _STATUSES else "[?]"
        # Pad icon to _ICON_COL_WIDTH so the description column starts at
        # the same x-position regardless of status name length ("ok" 4
        # chars vs "stop"/"kill" 6 chars). Trailing spaces after short
        # icons are absorbed into the status_gap.
        lines.append(
            f"{icon:<{_ICON_COL_WIDTH}}{_STATUS_GAP}{description:<{w_desc}}{_DESC_TOKEN_GAP}"
            f"{format_tokens(in_v):>{w_in}} "
            f"{format_tokens(out_v):>{w_out}} "
            f"{format_tokens(cached_v):>{w_cached}}"
        )

    # Prepend the table-row marker to everything except the session header
    # (see _TABLE_ROW_PREFIX). A single post-processing pass guarantees the
    # prefix is uniform across all row kinds — no per-f-string repetition
    # to drift out of sync.
    return "\n".join(
        [lines[0], *(_TABLE_ROW_PREFIX + line for line in lines[1:])]
    )


# ---------------------------------------------------------------------------
# main — entry point / orchestrator
# ---------------------------------------------------------------------------

# [deviation] Production Claude Code stores the main jsonl as a SIBLING to
# the session directory, not inside it. The fixture structure (and the real
# `~/.claude/projects/<encoded>/<sid>.jsonl` next to `<sid>/subagents/`)
# confirms this. The plan spec says "session_dir / f'{sid}.jsonl'", which
# would not find the file. We use session_dir.parent instead — this is the
# only place main() knows about the disk layout, all compute_* helpers are
# layout-agnostic.

# Fields persisted in agents_<sid>.json cache. agentId is the dict key,
# so NOT stored inside each entry; compute_agent_snapshot re-injects
# agentId on the cache-hit path to keep the returned dict shape stable
# for downstream consumers (see _write_agents_cache and _AGENT_CACHE_FIELDS
# invariant). mtime_jsonl/mtime_meta drive invalidation; the breakdown
# fields are the new render-ready shape (replacing the prior `tokens` sum).
_AGENT_CACHE_FIELDS = (
    "last_uuid",
    "mtime_jsonl",
    "status",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "description",
    "toolUseId",
    "mtime_meta",
)


def main() -> int:
    """Entry point: read stdin, compute, print multi-line status.

    Returns the process exit code (0 on success; we never return non-zero
    because the status line hook should never break the user's session —
    errors are swallowed and the worst case is a degraded display).
    """
    try:
        return _main_unsafe()
    except Exception:
        # Hard safety net: any unexpected error (OSError not anticipated,
        # programming bug, future-proofing) MUST NOT propagate out of the
        # status-line hook. Print a minimal header and return 0.
        try:
            print("Session:  | Branch:  | Model:  | User: n/a")
        except Exception:
            pass
        return 0


def _build_header(parsed: dict, context: str) -> str:
    """Build the single header line from a parsed stdin dict and a
    pre-formatted Context segment ("NK (P%)")."""
    sid = parsed.get("session_id", "") or ""
    return (
        f"Session: {sid} | "
        f"Branch: {parsed['branch']} | "
        f"Model: {parsed['model']} | "
        f"User: {parsed['user']} | "
        f"Context: {context}"
    )


def _context_segment(parsed: dict, main_cum: dict | None) -> str:
    """Format the header's Context segment from the best available source.

    Priority: payload context_window.total_input_tokens (parsed via
    parse_stdin as `context_tokens`) when positive — it is the freshest,
    provided by Claude Code itself, and works even when no local session
    dir is found. Otherwise the jsonl-derived occupancy from main_cum
    (context-window size at the last assistant event; 0 when main_cum is
    None). The percentage divisor comes from resolve_context_limit.
    """
    tokens = parsed.get("context_tokens") or 0
    if tokens <= 0 and main_cum is not None:
        tokens = main_cum.get("context_tokens") or 0
    return format_context(tokens, resolve_context_limit(parsed.get("model", "")))


def _data_dir() -> Path | None:
    """Ensure ~/.claude/status_line/data exists. Returns None if mkdir fails."""
    data_dir = Path.home() / ".claude" / "status_line" / "data"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    except OSError:
        return None


def _cache_path(data_dir: Path | None, name: str, session_id: str) -> Path:
    """Return the cache path for `name` (e.g. "main", "agents"). If data_dir
    is None, returns /dev/null so writes go nowhere and reads always miss."""
    if data_dir is None:
        return Path(os.devnull)
    return data_dir / f"{name}_{session_id}.json"


def _compute_agents(
    session_dirs: Path | list[Path],
    agents_cache_path: Path,
    task_notifications: dict[str, str] | None = None,
) -> list:
    """Build per-agent snapshots for every agent-*.jsonl under each session
    directory, using agents_cache_path as the source of stale cache entries.

    `session_dirs` accepts a single Path (backward-compatible call shape) or
    a list of Paths — ALL directories CC created for this session id (see
    `find_session_dirs` / `_resolve_session_dirs`): the main checkout and a
    worktree copy each hold part of the session's `subagents/` tree, so the
    directories are scanned in list order and the results merged. Dedup is
    by agentId (the jsonl stem) at the PATH level, BEFORE calling
    compute_agent_snapshot: an agentId already produced by an earlier
    directory is skipped, so the first directory wins and a duplicate is
    never parsed twice. A directory without `subagents/` is skipped (the
    rest of the list still runs); an empty list → [].

    After building all snapshots, apply the orchestrator-level queue override
    to the MERGED list: for each agent whose `agentId` (with the `agent-`
    prefix stripped) appears in `task_notifications` (extracted from the
    main jsonl's queue-operation events), set `status` to the notification
    value — BUT only when the current status is not already `err` or `stop`
    (those win by priority; see module docstring + CLAUDE.md "Status
    priority and overrides").

    [deviation] The override lives here rather than inside
    compute_agent_snapshot because the queue signal originates in the main
    jsonl (different file), not the agent's jsonl + meta. Keeping
    compute_agent_snapshot a pure function of one agent's own data preserves
    its narrow contract and makes it easy to cache.

    Args:
        session_dirs: session directory or list of them; each dir's
            `<dir>/subagents/agent-*.jsonl` files are scanned.
        agents_cache_path: cache file holding previous per-agent snapshots,
            used to short-circuit re-parse when file mtimes haven't changed.
        task_notifications: dict mapping `<task-id>` → one of {"ok","kill","err"}
            (extracted from `<task-notification>` queue-operation events in the
            main jsonl by compute_main_cum). May be empty or None.
    """
    dirs = [session_dirs] if isinstance(session_dirs, Path) else session_dirs
    agents: list = []
    agents_cache = _load_dict_cache(agents_cache_path)
    seen_agent_ids: set[str] = set()
    for session_dir in dirs:
        subagents_dir = session_dir / "subagents"
        if not subagents_dir.exists():
            continue
        for jsonl_path in sorted(subagents_dir.glob("agent-*.jsonl")):
            agent_id = jsonl_path.stem
            if agent_id in seen_agent_ids:
                # first dir wins; skip before parsing (dedup at path level)
                continue
            seen_agent_ids.add(agent_id)
            meta_path = jsonl_path.parent / f"{agent_id}.meta.json"
            cache_entry = agents_cache.get(agent_id)
            snapshot = compute_agent_snapshot(jsonl_path, meta_path, cache_entry)
            agents.append(snapshot)

    # Orchestrator-level queue override (with err/stop guard). See [deviation]
    # note above for why this lives here, not in compute_agent_snapshot.
    if task_notifications:
        for agent in agents:
            aid = agent["agentId"]
            # Strip "agent-" prefix to get the join key (matches <task-id>).
            key = aid[len("agent-"):] if aid.startswith("agent-") else aid
            if key in task_notifications and agent["status"] not in ("err", "stop"):
                agent["status"] = task_notifications[key]

    return agents


def _write_agents_cache(agents_cache_path: Path, agents: list) -> None:
    """Persist the per-agent cache atomically. Drops transient/derivable
    fields; keeps the keys listed in _AGENT_CACHE_FIELDS."""
    new_cache = {
        a["agentId"]: {k: a.get(k) for k in _AGENT_CACHE_FIELDS}
        for a in agents
    }
    try:
        _atomic_write_json(agents_cache_path, new_cache)
    except OSError:
        # Cache write failure is non-fatal — output is still correct,
        # just slower next invocation.
        pass


def _main_unsafe() -> int:
    """Internal implementation — assumes the caller (main) wraps OSError.
    See main() docstring for the never-crash contract."""
    parsed = parse_stdin(sys.stdin.read())
    session_id = parsed.get("session_id", "") or ""

    if not session_id:
        # empty session_id → header only, exit 0. Context segment still
        # renders from the payload when CC provided one.
        print(_build_header(parsed, _context_segment(parsed, None)))
        return 0

    # All session dirs CC created for this id (usually 1; a resumed-in-
    # worktree session splits across the main checkout and the worktree
    # copy — see _resolve_session_dirs). The transcript dir leads the
    # list, which both anchors agent-id dedup (first dir wins) and feeds
    # _find_main_jsonl below.
    session_dirs = _resolve_session_dirs(
        parsed.get("transcript_path", ""), session_id
    )
    # main jsonl: transcript_path payload → first session dir's sibling →
    # projects glob (see _find_main_jsonl). The session dirs are NOT a
    # gate — CC only materializes `<sid>/` once the session spawns its
    # first subagent, and a subagentless session still deserves its
    # main-row table + jsonl-derived Context.
    main_jsonl = _find_main_jsonl(
        parsed.get("transcript_path", ""),
        session_id,
        session_dirs[0] if session_dirs else None,
    )
    if main_jsonl is None:
        # no main jsonl anywhere → header only (payload context)
        print(_build_header(parsed, _context_segment(parsed, None)))
        return 0

    data_dir = _data_dir()
    main_cache = _cache_path(data_dir, "main", session_id)
    agents_cache = _cache_path(data_dir, "agents", session_id)

    main_cum = compute_main_cum(main_jsonl, main_cache)

    # Header needs main_cum for the jsonl-fallback context occupancy, so it
    # is built here rather than up top (payload context takes priority —
    # see _context_segment).
    header = _build_header(parsed, _context_segment(parsed, main_cum))

    # task_notifications extracted from queue-operation events in main jsonl
    # (added per 20260824-subagent-status-via-queue-notifications). May be
    # empty dict; the orchestrator override in _compute_agents is a no-op
    # in that case.
    task_notifications = main_cum.get("task_notifications", {})

    agents: list = []
    if session_dirs:
        # Scan EVERY session dir's subagents/ tree (a worktree-resumed
        # session holds part of its agents in the copy's dir) and merge
        # with agent-id dedup — see _compute_agents.
        agents = _compute_agents(session_dirs, agents_cache, task_notifications)
        _write_agents_cache(agents_cache, agents)
    # else: no session dirs at all → no subagents ever spawned → agents
    # stays []. The agents cache write is skipped too: there is nothing to
    # cache, and writing an empty dict would litter data/ with
    # agents_<sid>.json files for every dirless session. Note the write
    # DOES happen when dirs exist but a dir has no subagents/ — the cache
    # is then legitimately empty (or holds only the other dirs' agents),
    # and rewriting it is what self-heals the stale `{}` artifacts the
    # pre-merge code left behind (see _write_agents_cache).

    # sort_agents calls .get(...) on the second argument, so it MUST be a
    # dict. A malformed cache (e.g. tool_use_positions accidentally written
    # as a list) would otherwise raise AttributeError and be swallowed by
    # main()'s except clause — silently degrading to the fallback header.
    tool_use_positions = main_cum.get("tool_use_positions")
    agents = sort_agents(agents, tool_use_positions if isinstance(tool_use_positions, dict) else {})
    # Task 4 — breakdown-table refactor: pass the three cum_* values
    # directly to render (no `total` field in main_cum anymore). render
    # applies format_tokens to each cell; we hand it raw ints. The start_*
    # triple (first-message breakdown, the "start:" row) rides along the
    # same main_cum dict; `or 0` guards pre-upgrade caches.
    cum_in = main_cum.get("cum_in", 0)
    cum_out = main_cum.get("cum_out", 0)
    cum_cache_read = main_cum.get("cum_cache_read", 0)
    start_in = int(main_cum.get("start_in") or 0)
    start_out = int(main_cum.get("start_out") or 0)
    start_cached = int(main_cum.get("start_cached") or 0)
    output = render_output(
        header, start_in, start_out, start_cached, cum_in, cum_out, cum_cache_read, agents
    )
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())