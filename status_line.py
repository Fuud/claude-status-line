"""status_line.py — Claude Code status line aggregation.

Module-level invariants:
- format_tokens handles non-negative ints; negative values clamp to "0".
- detect_status returns one of {"err", "stop", "ok", "run"}.
- parse_stdin never raises; it returns a dict with all keys present.
- provider_host / load_prices never raise: an unset or malformed
  ANTHROPIC_BASE_URL yields "" and an unreadable/invalid prices.json
  yields None (treated as "no prices" — no cost columns).
- compute_main_cum / compute_agent_snapshot never raise; OSError is
  swallowed so the hook cannot crash the parent session.
- compute_agent_snapshot returns CUMULATIVE per-agent totals
  (tokens_in/out/cached summed over all assistant events with usage,
  plus the `models` per-model breakdown) — not the last event's usage
  (agreed behavior change, plan 20260826-status-line-model-cost-columns).
- render_output renders the model/cost columns only when a prices dict
  is passed: prices=None reproduces the pre-model-columns layout
  byte-for-byte (one row per group, group totals). With prices, every
  group (sum/main/agent) expands to one row per model in first-appearance
  order; zero-token per-model records are skipped and a group left empty
  renders ONE zero row with an empty model cell (groups are never
  skipped). The start row is a reference row and never carries
  model/cost cells.
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
import urllib.parse
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
# prices.json: load / lookup / compute / format
# ---------------------------------------------------------------------------

# [decision] Bound to HOME (not to __file__): in production the module lives
# in ~/.claude/status_line/ anyway, but the HOME binding is what makes the
# subprocess integration tests hermetic — a fake HOME isolates the test from
# the user's real prices.json (monkeypatching cannot reach a child process).
_PRICES_PATH = Path.home() / ".claude" / "status_line" / "prices.json"


def provider_host() -> str:
    """Hostname of ANTHROPIC_BASE_URL, or "" when unset/invalid/non-str.

    The hook distinguishes providers by base-URL host (the shell wrappers
    like zai-glm-5.2-1m / claude-kimi-k3 differ by host), e.g.
    "https://api.z.ai/api/anthropic" → "api.z.ai". Any error → "".
    """
    raw = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not isinstance(raw, str):
        return ""
    try:
        return urllib.parse.urlparse(raw).hostname or ""
    except ValueError:
        return ""


def _is_num(value: object) -> bool:
    """True for int/float but not bool (JSON `true` is not a price)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_prices(path: Path) -> "dict[str, dict] | None":
    """Read prices.json → {key: price} or None.

    A price entry is {"in": float, "out": float, "cache": float,
    "per": int|float, "units": str}. None (the file is then treated as
    absent — no cost column) when: the file is missing, the JSON is
    broken, the payload is not a list, an element is not a dict, `model`
    is not a non-empty string, `per` is missing / non-numeric / <= 0, or
    a present in/out/cache price is non-numeric. Missing in/out/cache
    default to 0; missing (or non-str) units default to "". Duplicate
    keys: the last entry wins. Never raises, never writes to stderr.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, list):
        return None
    prices: dict = {}
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        model = entry.get("model")
        per = entry.get("per")
        if not isinstance(model, str) or not model:
            return None
        if not _is_num(per) or per <= 0:
            return None
        record: dict = {"per": per, "units": entry.get("units", "")}
        if not isinstance(record["units"], str):
            record["units"] = ""
        for key in ("in", "out", "cache"):
            value = entry.get(key, 0)
            if not _is_num(value):
                return None
            record[key] = float(value)
        prices[model] = record
    return prices


def price_for(model: str, prices: "dict[str, dict] | None", host: str) -> "dict | None":
    """Price entry for a model id, or None when no price is known.

    Lookup chain: when host != "" try "model@host" first, then the bare
    "model", then give up. Callers map "price found → number, model
    known but no price → n/a".
    """
    if not prices:
        return None
    if host:
        entry = prices.get(f"{model}@{host}")
        if entry is not None:
            return entry
    return prices.get(model)


def compute_cost(tokens: dict, price: dict) -> float:
    """cost = (in·p_in + out·p_out + cached·p_cache) / per.

    `tokens` is a per-model accumulation record ({"in", "out", "cached"}
    token counts); `price` a load_prices record. cache_creation is
    deliberately not part of the formula (it is not displayed anywhere).
    """
    return (
        tokens.get("in", 0) * price.get("in", 0.0)
        + tokens.get("out", 0) * price.get("out", 0.0)
        + tokens.get("cached", 0) * price.get("cache", 0.0)
    ) / price["per"]


def format_cost(value: float, units: str) -> str:
    """Format a cost for the table's cost cell.

    Precision buckets: >= 1e6 → "X.XM"; >= 1000 → "X.Xk";
    0.1 <= v < 1000 → one decimal with a trailing ".0" stripped ("402");
    < 0.1 → two decimals ("0.04"). Units whose first char is not alnum
    glue as a prefix ("$8.1"); otherwise they append after a space
    ("402 credits"); empty units → the bare number.
    """
    if value >= 1_000_000:
        num = f"{value / 1_000_000:.1f}M"
    elif value >= 1000:
        num = f"{value / 1_000:.1f}k"
    elif value >= 0.1:
        num = f"{value:.1f}"
        if num.endswith(".0"):
            num = num[:-2]
    else:
        num = f"{value:.2f}"
    if not units:
        return num
    if units[0].isalnum():
        return f"{num} {units}"
    return f"{units}{num}"


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

# [decision] We read whole files (line iteration over the open handle)
# rather than mmap or reverse-chunked reads. Per-session jsonl files are
# small (sub-MB for typical agent activity, ~1.7 MB for the f5044e4f
# main jsonl, individual subagent jsonl files are tens of KB). A more
# elaborate mmap implementation would add complexity without measurable
# benefit at current sizes — revisit if profiling shows these reads as
# hot.


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
        per_model — model_id → {"in", "out", "cached"} token sums over that
            model's assistant events WITH a usage block (model id from
            message.model; `""` when the event carries no model field).
            Zero-token records — including `<synthetic>` — are KEPT here;
            skipping zero rows is a render concern (see render_output).
            Key order follows first appearance in the scan. `cached` is
            cache_read only — cache_creation is never surfaced, matching
            the cached-column semantics of every other row.
    """
    cum_in = cum_out = cum_cache_create = cum_cache_read = 0
    start_in = start_out = start_cached = 0
    context_tokens = 0
    tool_use_positions: dict[str, int] = {}
    task_notifications: dict[str, str] = {}
    per_model: dict[str, dict[str, int]] = {}
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
                    # Per-model breakdown (model/cost columns). Same gate
                    # as the cum_* sums: only assistant events with a usage
                    # block. setdefault keeps the FIRST-appearance key
                    # order render relies on; zero-token models (including
                    # <synthetic>) stay in the dict — the render layer
                    # decides which rows to show.
                    model_id = str(msg.get("model") or "")
                    model_rec = per_model.setdefault(
                        model_id, {"in": 0, "out": 0, "cached": 0}
                    )
                    model_rec["in"] += in_v
                    model_rec["out"] += out_v
                    model_rec["cached"] += cache_read_v
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
        "per_model": per_model,
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
    "per_model": {},
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
        last_uuid, mtime_jsonl, tool_use_positions, task_notifications,
        per_model

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

    [deviation] Cache hit likewise requires `per_model` to be present:
    pre-model-column caches lack it and would render empty model/cost
    columns for one cycle after upgrade. Same field-presence guard pattern
    as the context_tokens / start_* checks above.

    per_model is the per-model token breakdown feeding the table's model
    and cost columns (see _scan_main_jsonl for the accumulation rules).

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
    # `context_tokens`, the `start_*` fields and `per_model` presence are
    # part of the hit check (see [deviation]s in the docstring): pre-upgrade
    # caches lack them.
    if (
        cache is not None
        and scan["last_uuid"]
        and cache.get("last_uuid") == scan["last_uuid"]
        and cache.get("mtime_jsonl") == mtime_jsonl
        and "context_tokens" in cache
        and all(f in cache for f in ("start_in", "start_out", "start_cached"))
        and "per_model" in cache
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


# [decision] _scan_agent_jsonl scans the agent jsonl FORWARD, once, on
# every call — including cache hits. The scan has to run before the
# cache-key comparison anyway (last_uuid comes from the scan itself), and
# the cumulative totals + per-model breakdown it produces cannot be
# derived from the reverse tail read the old _read_last_event helper did.
# The I/O is unchanged (that helper also slurped the whole file via
# readlines()); what grows is the json.loads work — from "the lines after
# the last assistant event" (reverse early-exit) to every line — on
# files that are tens of KB. Same trade-off compute_main_cum's single
# forward scan already documents; accepted so agent rows show honest
# cumulative per-model totals (plan 20260826-status-line-model-cost-columns).
def _scan_agent_jsonl(jsonl_path: Path) -> dict:
    """Single forward scan of one subagent jsonl.

    Returns a dict with keys:
        tokens_in, tokens_out, tokens_cached
            — cumulative sums of input/output/cache_read tokens across
              ALL assistant events that carry a usage block
              (cache_creation is NOT surfaced, matching every other
              row's cached-column semantics).
        models — model_id → {"in","out","cached"} accumulated over the
              same events (model id from message.model, "" when the
              event carries no model field). Zero-token records —
              including <synthetic> — are KEPT here; skipping zero rows
              is a render concern. Key order follows first appearance.
        last_uuid — uuid field of the LAST assistant event, None when
              there is no assistant event (or it carries no uuid).
        last_assistant — the last assistant event dict itself, or None.
        last_event — the very last parsable event of ANY type, or None —
              feeds detect_status (e.g. a user "[Request interrupted by
              user]" event written after the final assistant must
              surface as "stop").

    A missing/unreadable file, or an OSError mid-read, yields the
    all-zero empty scan (the degradation _read_last_event used to
    provide — the hook cannot crash the parent session).
    """
    tokens_in = tokens_out = tokens_cached = 0
    models: dict[str, dict[str, int]] = {}
    last_assistant: dict | None = None
    last_event: dict | None = None
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # partial line — race with the subagent writing; skip
                    continue
                if not isinstance(event, dict):
                    continue
                last_event = event
                if event.get("type") != "assistant":
                    continue
                last_assistant = event
                msg = event.get("message") or {}
                usage = msg.get("usage") if isinstance(msg, dict) else None
                if not isinstance(usage, dict):
                    continue
                in_v = int(usage.get("input_tokens", 0) or 0)
                out_v = int(usage.get("output_tokens", 0) or 0)
                cached_v = int(usage.get("cache_read_input_tokens", 0) or 0)
                tokens_in += in_v
                tokens_out += out_v
                tokens_cached += cached_v
                # Same gate and setdefault-first-appearance pattern as
                # _scan_main_jsonl's per_model; zero-token models stay.
                model_id = str(msg.get("model") or "")
                model_rec = models.setdefault(
                    model_id, {"in": 0, "out": 0, "cached": 0}
                )
                model_rec["in"] += in_v
                model_rec["out"] += out_v
                model_rec["cached"] += cached_v
    except OSError:
        return {
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cached": 0,
            "models": {},
            "last_uuid": None,
            "last_assistant": None,
            "last_event": None,
        }
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_cached": tokens_cached,
        "models": models,
        "last_uuid": (
            last_assistant.get("uuid") if last_assistant is not None else None
        ),
        "last_assistant": last_assistant,
        "last_event": last_event,
    }


def compute_agent_snapshot(
    jsonl_path: Path, meta_path: Path, cache_entry: dict | None
) -> dict:
    """Return snapshot dict for a single subagent.

    Returns a dict with keys:
        agentId       — jsonl filename without `.jsonl` extension
        status        — one of {"ok","err","stop","run"} (see detect_status)
        tokens_in     — cumulative input_tokens across ALL assistant events
                        with a usage block (0 when there are none)
        tokens_out    — cumulative output_tokens, same accumulation rules
        tokens_cached — cumulative cache_read_input_tokens, same rules.
                        cache_creation_input_tokens is NOT surfaced.
        models        — per-model breakdown {model_id: {"in","out","cached"}}
                        over the same events ({} when no assistant event
                        carries usage) — see _scan_agent_jsonl.
        description   — meta.description, truncated to 40 chars with "…".
                        Falls back to meta.agentType, then "unknown".
        toolUseId     — meta.toolUseId (string; "" if missing)
        last_uuid     — uuid of the last assistant event, or None
        mtime_jsonl   — st_mtime of jsonl_path, or 0.0 if missing
        mtime_meta    — st_mtime of meta_path, or 0.0 if missing

    [deviation vs the pre-model-columns schema] The breakdown fields are
    CUMULATIVE totals over the whole jsonl, not the last assistant
    event's usage — agent rows show the agent's total spend, and `sum:`
    becomes an honest session total. Agreed visible behavior change (see
    plan 20260826-status-line-model-cost-columns, Overview).

    Breakdown fields (tokens_* / models) are ALWAYS populated, even for
    status="run" (mid-flow) — the user sees totals so far, not blanks.
    Agents with no assistant events or no usage blocks get zeros and an
    empty models dict.

    Cache hit: if `cache_entry` is provided AND its last_uuid AND
    mtime_jsonl AND mtime_meta match the current on-disk state AND all
    three breakdown fields AND `models` are present in cache_entry, the
    cache_entry is returned unchanged. The field-presence checks guard
    against stale pre-upgrade caches (which would render zeros via
    `int(a.get(field) or 0)` until the next jsonl mutation). This
    function does NOT write to any cache file — the caller (orchestrator)
    owns cache persistence.

    [deviation] When the jsonl contains zero assistant events at all, status
    is forced to "err" (or "stop" if meta.stoppedByUser=true) regardless of
    what detect_status would return. See module-level note above.
    """
    # 1. mtime_jsonl (0.0 if missing).
    mtime_jsonl = _jsonl_mtime(jsonl_path)

    # 2. Single forward scan — cumulative totals, per-model breakdown,
    # last assistant uuid, and the last event of any type. That last
    # event drives status detection (e.g. a user "[Request interrupted
    # by user]" event written AFTER the final assistant must surface as
    # "stop").
    scan = _scan_agent_jsonl(jsonl_path)

    # 3. Load meta ({} on any failure).
    meta = _load_meta_dict(meta_path)

    # 4. Cache hit check. mtime_meta is part of the key: if meta.json
    # mutates (e.g. stoppedByUser added later, description edited),
    # cache must invalidate even if jsonl mtime+uuid are unchanged.
    # Field-presence checks for the three breakdown fields and `models`
    # are REQUIRED: a pre-upgrade cache (old format) would otherwise
    # satisfy the key-match but lack the new fields, leading to render
    # zeros / empty model cells until the next jsonl mutation.
    mtime_meta_for_compare = _meta_mtime(meta_path)
    last_uuid_for_compare = scan["last_uuid"]
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
            and "models" in cache_entry
        ):
            # Preserve the invariant: the returned snapshot always has
            # `agentId` inside, regardless of cache hit or miss. The
            # on-disk cache stores agentId as the dict key (see
            # _AGENT_CACHE_FIELDS), so we re-inject it here from the
            # canonical source (jsonl_path.stem).
            return {**cache_entry, "agentId": agent_id}

    # 5. Compute fields.

    # status — apply "0 assistant events → err" override. detect_status
    # otherwise inspects the very last event of any type (falls back to
    # the last assistant event in the degenerate no-parsable-events
    # case, which in practice implies no assistant events at all).
    last_assistant = scan["last_assistant"]
    if last_assistant is None:
        # No assistant events at all in the jsonl.
        if meta.get("stoppedByUser") is True:
            status = "stop"
        else:
            status = "err"
    else:
        detect_input = (
            scan["last_event"] if scan["last_event"] is not None else last_assistant
        )
        status = detect_status(detect_input, meta)

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
        "tokens_in": scan["tokens_in"],
        "tokens_out": scan["tokens_out"],
        "tokens_cached": scan["tokens_cached"],
        "models": scan["models"],
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

    Globs `<projects_root>/*/<session_id>` (one level) plus a root-level
    `<projects_root>/<session_id>` check, and returns every matching
    *directory* as a list of Paths — root-level match first, then glob
    order (OS-dependent, but stable per tree). Returns [] if
    `session_id` is empty, if `projects_root` does not exist, or if
    no matching directory is found.

    The same session id can legitimately live in more than one encoded
    project directory — e.g. the main checkout and a worktree copy of the
    same repo, each with its own `subagents/` tree. Callers that need the
    complete picture (agents, tokens) must merge results across ALL of
    these directories, which is why this exists alongside the historical
    single-match `find_session_dir`.

    If `projects_root` is None, defaults to `<home>/.claude/projects`.

    The `projects_root` parameter mirrors `find_session_dir`'s parameter
    contract; tests can also drive this function via
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
    # [deviation] The glob is one level (`*/`), not recursive (`**/`):
    # the on-disk convention is `<encoded-project>/<session_id>/` with
    # encoded project dirs as direct children of projects/ (verified on
    # the real tree: every session dir sits at depth 1), and a recursive
    # walk would descend into every session dir's `subagents/` and
    # `tool-results/` subtrees for matches the convention never produces
    # — ~110ms vs ~6ms per hook invocation on the real tree. The bare
    # root-level check below covers the zero-directory case that `**`
    # additionally matched (`<projects_root>/<session_id>`). The
    # one-level glob does not recurse through symlinked subtrees the way
    # a full `**` walk would, though it does resolve `<session_id>`
    # through a direct-child symlinked project dir (one level deep, no
    # further).
    matches: list[Path] = []
    root_level = projects_root / session_id
    if root_level.is_dir():
        matches.append(root_level)
    matches.extend(d for d in projects_root.glob(f"*/{session_id}") if d.is_dir())
    return matches


def find_session_dir(
    session_id: str, projects_root: Path | None = None
) -> Path | None:
    """Locate the FIRST directory named `session_id` under `projects_root`.

    Thin wrapper over `find_session_dirs`: returns the first element of
    its result (root-level match first, then glob order), or None when
    the list is empty — `session_id`
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

    Starts from `find_session_dirs` (every `*/<session_id>` directory under
    `projects_root`) and, when `transcript_path` is non-empty and
    `Path(transcript_path).parent / session_id` is an existing directory,
    moves that directory to the front of the list (without duplicating it
    if glob already returned it — matched by filesystem identity, not path
    spelling; see `_same_file`). Backslashes in `transcript_path` are
    normalized to forward slashes first: Windows CC sends `C:\\...` paths,
    and under a posix-flavoured python (cygwin) posixpath would otherwise
    treat the whole string as one component, making `.parent` degenerate
    to "." and the priority below silently never engage.

    Priority rationale: transcript_path is CC's own authoritative statement
    of where the session lives (the same source `_find_main_jsonl` trusts
    first). The first entry of the result wins agent-id dedup downstream,
    so the authoritative directory must lead even when glob's OS-dependent
    ordering would put an empty worktree copy first (the bug this fixes).

    Degradations: empty `transcript_path`, or one whose sibling session dir
    does not exist on disk, yields the pure `find_session_dirs` order
    unchanged. Empty
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
    # The transcript dir may already be among the glob matches under a
    # DIFFERENT spelling: under the cygwin production interpreter glob
    # results are rooted at Path.home() (/cygdrive/c/Users/...) while the
    # normalized transcript path spells the same directory C:/Users/...
    # PurePath equality compares spellings, so the two never compare equal
    # — match by filesystem identity instead and move the glob-spelling
    # twin to the front (one entry per physical directory, spelled the
    # glob way — the spelling every downstream scan already exercised).
    twin = next((d for d in dirs if _same_file(d, preferred)), None)
    if twin is None:
        # only reachable when the transcript dir sits outside the globbed
        # tree — no twin to reorder, so prepend the transcript spelling
        return [preferred] + dirs
    return [twin] + [d for d in dirs if d is not twin]


def _same_file(a: Path, b: Path) -> bool:
    """True when `a` and `b` name the same existing filesystem object.

    Spelling equality is checked first (cheap, no syscalls); otherwise
    os.path.samefile resolves both through the OS — under the cygwin
    production interpreter it sees through /cygdrive/c/... vs C:/...
    spellings of the same directory. NOTE: Path.resolve()/realpath is NOT
    a usable canonicalization here — posixpath treats "C:/..." as
    relative and would prepend the CWD to it.
    """
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        # either path vanished between the caller's is_dir()/glob() check
        # and now — treat as different rather than crash the hook
        return False


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

    [deviation] The glob is one level (`*/`), not recursive (`**/`), for
    the same reason as `find_session_dirs`' `*/<sid>` glob above — the
    on-disk convention is `<encoded-project>/<sid>.jsonl` with encoded
    project dirs as direct children of `projects/`, and a recursive walk
    would descend into every session dir (incl. `subagents/` trees) for
    no gain; see that function's `[deviation]` note for the measured
    cost of the recursive variant.

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

    [decision] Retained (and still exported) after render_table
    generalized the width computation to arbitrary columns of
    pre-formatted cells: tests/test_render_output.py imports it to
    reconstruct expected header strings, and it documents the token-
    column floor formula (floor _TOKEN_COLUMN_WIDTH, label, widest
    formatted cell). render_table applies the same max() to its own
    column specs.

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


# Floor for the label/description column: an agent row's minimum
# footprint — the icon padded to _ICON_COL_WIDTH plus the status gap.
# Together with the column's "gap" (the 2-space _DESC_TOKEN_GAP) this
# reproduces the pre-model-columns header_pad = w_desc + _ICON_COL_WIDTH
# + 4 exactly, keeping the prices=None layout byte-identical.
_LABEL_COL_FLOOR = _ICON_COL_WIDTH + len(_STATUS_GAP)


def _cell_text(row: list, index: int) -> str:
    """Cell at `index` of a render_table row as a string — "" for a
    missing cell (ragged row) or an explicit None."""
    cell = row[index] if index < len(row) else ""
    return "" if cell is None else str(cell)


def render_table(columns: list, rows: list) -> list:
    """Render a table as a list of lines WITHOUT the "| " prefix.

    `columns` is a list of column dicts:
        {"label": str, "align": "left"|"right", "floor": int,
         "gap": str (optional, default " ")}
    `rows` is a list of rows, each a list of pre-formatted (string)
    cells — render_table does no number formatting of its own.

    The first returned line is the LABEL row built from the columns'
    labels; then one line per row. Column width =
        max(floor, len(label), longest cell in that column).
    Left-aligned cells pad on the right (ljust), right-aligned on the
    left (rjust); the column's "gap" is glued after it. Ragged rows
    render missing cells as empty. Every line is right-stripped — an
    empty cell in the last column leaves no trailing padding spaces
    (trailing whitespace is never meaningful in this table).
    """
    widths = []
    for index, column in enumerate(columns):
        longest = max(
            (len(_cell_text(row, index)) for row in rows), default=0
        )
        widths.append(
            max(
                int(column.get("floor", 0) or 0),
                len(column.get("label", "")),
                longest,
            )
        )
    label_row = [column.get("label", "") for column in columns]
    lines = []
    for row in [label_row, *rows]:
        parts = []
        for index, column in enumerate(columns):
            cell = _cell_text(row, index)
            if column.get("align", "left") == "right":
                cell = cell.rjust(widths[index])
            else:
                cell = cell.ljust(widths[index])
            parts.append(cell)
            parts.append(column.get("gap", " "))
        lines.append("".join(parts).rstrip())
    return lines


def _cost_cell(
    model: str, rec: dict, prices: "dict | None", host: str
) -> str:
    """Cost cell for one per-model row: the formatted number when a price
    is found, "n/a" when the model is known but unpriced, "" when there
    is no model at all (the start row and the zero-fallback rows — no
    model means nothing to price)."""
    if not model:
        return ""
    price = price_for(model, prices, host)
    if price is None:
        return "n/a"
    return format_cost(compute_cost(rec, price), price.get("units", ""))


def _models_total(models: "dict | None") -> tuple:
    """(in, out, cached) summed over a per-model dict's records.
    Non-dict records are skipped defensively."""
    total_in = total_out = total_cached = 0
    for rec in (models or {}).values():
        if not isinstance(rec, dict):
            continue
        total_in += int(rec.get("in") or 0)
        total_out += int(rec.get("out") or 0)
        total_cached += int(rec.get("cached") or 0)
    return total_in, total_out, total_cached


def _merge_models(sources: list) -> dict:
    """Merge per-model dicts into one, summing per model (no cross-model
    aggregation). Key order = first appearance across `sources` — pass
    them in render order (main first, then agents)."""
    merged: dict = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for model, rec in source.items():
            if not isinstance(rec, dict):
                continue
            acc = merged.setdefault(model, {"in": 0, "out": 0, "cached": 0})
            acc["in"] += int(rec.get("in") or 0)
            acc["out"] += int(rec.get("out") or 0)
            acc["cached"] += int(rec.get("cached") or 0)
    return merged


def _group_model_rows(
    label: str, models: "dict | None", prices: "dict | None", host: str
) -> list:
    """Wide rows (label, model, in, out, cached, cost) for one group.

    One row per model in first-appearance order; per-model records whose
    tokens are ALL zero (e.g. <synthetic>) are skipped entirely. The
    label (sum:/main:/icon+description) rides only the FIRST row. A group
    left with no rows after the skip renders ONE zero row with an EMPTY
    model cell — groups are never skipped (the "agents never skipped"
    invariant, extended to main and sum).
    """
    rows: list = []
    for model, rec in (models or {}).items():
        if not isinstance(rec, dict):
            continue
        in_v = int(rec.get("in") or 0)
        out_v = int(rec.get("out") or 0)
        cached_v = int(rec.get("cached") or 0)
        if not (in_v or out_v or cached_v):
            continue
        rows.append(
            [
                label,
                model,
                format_tokens(in_v),
                format_tokens(out_v),
                format_tokens(cached_v),
                _cost_cell(model, rec, prices, host),
            ]
        )
        label = ""
    if not rows:
        rows.append([label, "", "0", "0", "0", ""])
    return rows


def render_output(
    header: str,
    start_in: int,
    start_out: int,
    start_cached: int,
    main_models: dict,
    agents: list,
    prices: "dict | None" = None,
    host: str = "",
) -> str:
    """Build the multi-line status line string with a tabular breakdown.

    Layout with prices (the model + cost columns are shown):
        <header>
        | <table header — labels "model"/"in"/"out"/"cached"/"cost";
          the label/description column's label is EMPTY>
        | start:                                     <in> <out> <cached>
        | sum:   <model> <in> <out> <cached> <cost>   # only if agents
        | main:  <model> <in> <out> <cached> <cost>
        | for each agent (in input order):
              [<status>]  <description>  <model> <in> <out> <cached> <cost>

    Layout with prices=None is byte-identical to the pre-model-columns
    render: NO model and NO cost columns, one row per group carrying the
    group's totals (backward-compatibility requirement — a missing
    prices.json must not change the table's shape).

    main_models is the per-model breakdown {model_id: {"in","out","cached"}}
    (see _scan_main_jsonl); the main row's totals are the sum of its
    records. `prices` is a load_prices() dict, `host` a provider_host()
    string — the orchestrator wires both (Task 5); the model column sits
    between the description and `in` (left-aligned), the cost column
    after `cached` (right-aligned).

    Groups with prices: sum (per-model merge of main_models and every
    agent's `models`, NO cross-model sums, model order = first appearance
    with main first), main, and each agent expand to one row PER MODEL;
    per-model records with all-zero tokens (e.g. <synthetic>) are skipped
    entirely; a group left with no rows renders ONE zero row with an
    EMPTY model cell — groups (and therefore agents) are never skipped.

    The start row is the FIRST table row: the first assistant event's
    breakdown (the session's baseline message). It is a reference row —
    not part of the sum row, never carrying model/cost cells — and is
    always rendered, like the main row, even when all zeros.

    Every table row carries the "| " prefix (_TABLE_ROW_PREFIX) so that
    Claude Code's leading-whitespace strip cannot left-shift the
    all-spaces token-header row relative to the label/icon rows below.

    All padding/alignment is delegated to render_table; the label column
    (label/description/icon) is left-aligned with floor
    _LABEL_COL_FLOOR, token columns right-aligned with floor
    _TOKEN_COLUMN_WIDTH (= _col_width's floor). Description is truncated
    to 40 chars with U+2026 (re-applied defensively). Defensive
    `int(... or 0)` / isinstance handling covers pre-upgrade caches and
    hand-built snapshots.
    """
    # 1. Project agents into render-ready rows: the group label (icon +
    # status gap + truncated description — only the group's FIRST row
    # shows it), the flat totals (prices=None path), and the per-model
    # breakdown (prices path).
    projected: list[dict] = []
    for a in agents:
        status = a.get("status", "run")
        icon = f"[{status}]" if status in _STATUSES else "[?]"
        # Pad icon to _ICON_COL_WIDTH so the description column starts at
        # the same x-position regardless of status name length ("ok" 4
        # chars vs "stop"/"kill" 6 chars). Trailing spaces after short
        # icons are absorbed into the status_gap.
        models = a.get("models")
        projected.append(
            {
                "label": (
                    f"{icon:<{_ICON_COL_WIDTH}}{_STATUS_GAP}"
                    f"{_truncate_description(a.get('description', '') or '')}"
                ),
                "in": int(a.get("tokens_in") or 0),
                "out": int(a.get("tokens_out") or 0),
                "cached": int(a.get("tokens_cached") or 0),
                "models": models if isinstance(models, dict) else {},
            }
        )
    if not isinstance(main_models, dict):
        main_models = {}

    # 2. Column specs + rows. The label column's gap is the historical
    # 2-space description gap; token columns keep the single-space
    # separators. prices=None drops the model/cost columns entirely.
    label_column: dict = {
        "label": "",
        "align": "left",
        "floor": _LABEL_COL_FLOOR,
        "gap": _DESC_TOKEN_GAP,
    }
    rows: list = []
    if prices is None:
        # Today's layout: one row per group, the group's totals.
        columns = [
            label_column,
            {"label": "in", "align": "right", "floor": _TOKEN_COLUMN_WIDTH, "gap": " "},
            {"label": "out", "align": "right", "floor": _TOKEN_COLUMN_WIDTH, "gap": " "},
            {"label": "cached", "align": "right", "floor": _TOKEN_COLUMN_WIDTH},
        ]
        main_in, main_out, main_cached = _models_total(main_models)
        rows.append(
            [
                "start:",
                format_tokens(start_in),
                format_tokens(start_out),
                format_tokens(start_cached),
            ]
        )
        if projected:
            rows.append(
                [
                    "sum:",
                    format_tokens(main_in + sum(p["in"] for p in projected)),
                    format_tokens(main_out + sum(p["out"] for p in projected)),
                    format_tokens(main_cached + sum(p["cached"] for p in projected)),
                ]
            )
        rows.append(
            [
                "main:",
                format_tokens(main_in),
                format_tokens(main_out),
                format_tokens(main_cached),
            ]
        )
        for p in projected:
            rows.append(
                [
                    p["label"],
                    format_tokens(p["in"]),
                    format_tokens(p["out"]),
                    format_tokens(p["cached"]),
                ]
            )
    else:
        # Model column between description and `in`; cost column after
        # `cached` (2-space gaps on both sides of the token block, so
        # the extra columns read as additions to the old layout).
        columns = [
            label_column,
            {"label": "model", "align": "left", "floor": 0, "gap": _DESC_TOKEN_GAP},
            {"label": "in", "align": "right", "floor": _TOKEN_COLUMN_WIDTH, "gap": " "},
            {"label": "out", "align": "right", "floor": _TOKEN_COLUMN_WIDTH, "gap": " "},
            {"label": "cached", "align": "right", "floor": _TOKEN_COLUMN_WIDTH, "gap": _DESC_TOKEN_GAP},
            {"label": "cost", "align": "right", "floor": 0},
        ]
        rows.append(
            [
                "start:",
                "",
                format_tokens(start_in),
                format_tokens(start_out),
                format_tokens(start_cached),
                "",
            ]
        )
        if projected:
            merged = _merge_models(
                [main_models, *(p["models"] for p in projected)]
            )
            rows.extend(_group_model_rows("sum:", merged, prices, host))
        rows.extend(_group_model_rows("main:", main_models, prices, host))
        for p in projected:
            rows.extend(_group_model_rows(p["label"], p["models"], prices, host))

    # 3. Render the table and prepend the row marker to everything
    # except the session header (see _TABLE_ROW_PREFIX). A single
    # post-processing pass guarantees the prefix is uniform across all
    # row kinds — no per-f-string repetition to drift out of sync.
    table_lines = render_table(columns, rows)
    return "\n".join(
        [header, *(_TABLE_ROW_PREFIX + line for line in table_lines)]
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
# fields are the render-ready shape (replacing the prior `tokens` sum);
# `models` is the per-model breakdown feeding the model/cost columns —
# its presence is part of the cache-hit check so pre-model-columns
# caches are rebuilt once and rewritten.
_AGENT_CACHE_FIELDS = (
    "last_uuid",
    "mtime_jsonl",
    "status",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "models",
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
    session_dirs: Path | str | os.PathLike | list[Path],
    agents_cache_path: Path,
    task_notifications: dict[str, str] | None = None,
) -> list:
    """Build per-agent snapshots for every agent-*.jsonl under each session
    directory, using agents_cache_path as the source of stale cache entries.

    `session_dirs` accepts a single directory (Path/str — the
    backward-compatible call shape) or a list of Paths — ALL directories CC
    created for this session id (see `find_session_dirs` /
    `_resolve_session_dirs`): the main checkout and a worktree copy each
    hold part of the session's `subagents/` tree, so the
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
    (those win by priority; see the module docstring's invariant on the
    orchestrator override).

    [deviation] The override lives here rather than inside
    compute_agent_snapshot because the queue signal originates in the main
    jsonl (different file), not the agent's jsonl + meta. Keeping
    compute_agent_snapshot a pure function of one agent's own data preserves
    its narrow contract and makes it easy to cache.

    Args:
        session_dirs: session directory or list of them; each dir's
            `<dir>/subagents/agent-*.jsonl` files are scanned. A single
            directory may be a Path, a str, or any os.PathLike (all are
            normalized to a one-element list — a bare str would otherwise
            be iterated character by character and silently yield no
            agents).
        agents_cache_path: cache file holding previous per-agent snapshots,
            used to short-circuit re-parse when file mtimes haven't changed.
        task_notifications: dict mapping `<task-id>` → one of {"ok","kill","err"}
            (extracted from `<task-notification>` queue-operation events in the
            main jsonl by compute_main_cum). May be empty or None.
    """
    if isinstance(session_dirs, (str, os.PathLike)):
        dirs = [Path(session_dirs)]
    else:
        dirs = list(session_dirs)
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
    transcript_path = parsed.get("transcript_path", "") or ""

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
    session_dirs = _resolve_session_dirs(transcript_path, session_id)
    # main jsonl: transcript_path payload → first session dir's sibling →
    # projects glob (see _find_main_jsonl). The session dirs are NOT a
    # gate — CC only materializes `<sid>/` once the session spawns its
    # first subagent, and a subagentless session still deserves its
    # main-row table + jsonl-derived Context.
    main_jsonl = _find_main_jsonl(
        transcript_path,
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
    # stays [] and the cache write is skipped (an empty write would only
    # litter data/ for dirless sessions). With dirs present the write
    # happens even at 0 agents — that is what rewrites stale `{}` cache
    # artifacts (see "Edge cases" in README).

    # sort_agents calls .get(...) on the second argument, so it MUST be a
    # dict. A malformed cache (e.g. tool_use_positions accidentally written
    # as a list) would otherwise raise AttributeError and be swallowed by
    # main()'s except clause — silently degrading to the fallback header.
    tool_use_positions = main_cum.get("tool_use_positions")
    agents = sort_agents(agents, tool_use_positions if isinstance(tool_use_positions, dict) else {})
    # Task 4 — model/cost columns: render_output consumes the per-model
    # dict (the main row's totals are the sum of its records; cum_in /
    # cum_out / cum_cache_read are no longer passed flat). prices/host
    # wiring is the Task 5 orchestrator work — until then the hook
    # renders the no-prices layout. `or 0` / `or {}` guard pre-upgrade
    # caches.
    start_in = int(main_cum.get("start_in") or 0)
    start_out = int(main_cum.get("start_out") or 0)
    start_cached = int(main_cum.get("start_cached") or 0)
    main_models = main_cum.get("per_model") or {}
    output = render_output(
        header,
        start_in,
        start_out,
        start_cached,
        main_models,
        agents,
        prices=None,
        host="",
    )
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())