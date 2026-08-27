"""Tests for the header Context helpers.

resolve_context_limit(model) → the token limit the header percentage is
computed against:
    1. env CLAUDE_CODE_CONTEXT_LIMIT (positive int) — wins outright;
    2. "[1m]" substring in the model display name → 1_000_000;
    3. otherwise 200_000.

format_context(tokens, limit) → "NK (P%)": whole thousands, whole percent,
both round-half-to-even (round()).

Tests delenv/setenv explicitly so they don't depend on the developer's
environment having (or not having) CLAUDE_CODE_CONTEXT_LIMIT set.
"""
from __future__ import annotations

import pytest

from status_line import format_context, resolve_context_limit

ENV = "CLAUDE_CODE_CONTEXT_LIMIT"


# ---------------------------------------------------------------------------
# resolve_context_limit
# ---------------------------------------------------------------------------

def test_no_env_plain_model_is_200K(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert resolve_context_limit("claude-opus-4") == 200_000


def test_no_env_empty_model_is_200K(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert resolve_context_limit("") == 200_000


def test_1m_model_is_1m(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert resolve_context_limit("glm-5.3[1m]") == 1_000_000


def test_1m_match_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert resolve_context_limit("GLM-5.3[1M]") == 1_000_000


def test_1m_must_be_bracketed_suffix_not_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model merely containing "1m" without the bracket marker is NOT a
    1M-context model — e.g. "1m-token-mini" stays at the 200K default."""
    monkeypatch.delenv(ENV, raising=False)
    assert resolve_context_limit("1m-token-mini") == 200_000


def test_env_overrides_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "500000")
    assert resolve_context_limit("glm-5.3[1m]") == 500_000


def test_env_non_int_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "abc")
    assert resolve_context_limit("glm-5.3[1m]") == 1_000_000
    assert resolve_context_limit("plain") == 200_000


def test_env_zero_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "0")
    assert resolve_context_limit("plain") == 200_000


def test_env_negative_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "-100000")
    assert resolve_context_limit("plain") == 200_000


def test_env_empty_string_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "")
    assert resolve_context_limit("glm-5.3[1m]") == 1_000_000


# ---------------------------------------------------------------------------
# format_context
# ---------------------------------------------------------------------------

def test_format_context_zero() -> None:
    assert format_context(0, 200_000) == "0K (0%)"


def test_format_context_typical_values() -> None:
    # 15500 tokens vs 200K: round(15.5)=16 (half-to-even → 16), round(7.75)=8.
    assert format_context(15_500, 200_000) == "16K (8%)"
    # 154321 vs 1M: round(154.321)=154, round(15.4321)=15.
    assert format_context(154_321, 1_000_000) == "154K (15%)"
    # exactly half: 100K of 200K.
    assert format_context(100_000, 200_000) == "100K (50%)"


def test_format_context_sub_thousand_rounds_up() -> None:
    # 850 tokens → round(0.85) = 1 → "1K"; percent round(0.425) = 0.
    assert format_context(850, 200_000) == "1K (0%)"


def test_format_context_over_limit() -> None:
    # honest over-100% display, no clamping of the percentage.
    assert format_context(1_540_000, 1_000_000) == "1540K (154%)"


def test_format_context_negative_tokens_clamp() -> None:
    assert format_context(-5, 100_000) == "0K (0%)"


def test_format_context_non_positive_limit_no_crash() -> None:
    """Defensive: resolve_context_limit never returns <=0, but a hand-rolled
    caller passing 0 must get "K" with 0% rather than ZeroDivisionError."""
    assert format_context(1_234, 0) == "1K (0%)"
