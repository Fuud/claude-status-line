"""Tests for the prices.json pure functions (Task 1 of the model+cost plan).

Covers (from the plan's Technical Details):
- provider_host: ANTHROPIC_BASE_URL present / absent / malformed / non-str
- load_prices: missing file / broken JSON / not a list / missing model /
  bad `per` / non-numeric prices / partial fields / valid / duplicate keys
- price_for: model@host → model → None chain, host=""
- compute_cost: exact values, zero components
- format_cost: M / k / 1-decimal without trailing .0 / 2 decimals < 0.1;
  "$" prefix / "credits" suffix / empty units

Unit tests pass the path explicitly (load_prices(path)); only the
integration tests (Task 5) rely on _PRICES_PATH living under HOME.
"""
from __future__ import annotations

import json

import pytest

from status_line import (
    _PRICES_PATH,
    compute_cost,
    format_cost,
    load_prices,
    price_for,
    provider_host,
)


# ---------------------------------------------------------------------------
# provider_host
# ---------------------------------------------------------------------------

def test_provider_host_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic")
    assert provider_host() == "api.z.ai"


def test_provider_host_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert provider_host() == ""


def test_provider_host_malformed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # urlparse raises ValueError on an unterminated IPv6 literal — must be
    # swallowed into "" (the hook never crashes).
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://[::1")
    assert provider_host() == ""


def test_provider_host_non_str_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.environ", {"ANTHROPIC_BASE_URL": 123})
    assert provider_host() == ""


# ---------------------------------------------------------------------------
# load_prices
# ---------------------------------------------------------------------------

def _write(path, payload) -> None:
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def test_load_prices_missing_file(tmp_path) -> None:
    assert load_prices(tmp_path / "prices.json") is None


def test_load_prices_broken_json(tmp_path) -> None:
    p = tmp_path / "prices.json"
    p.write_text("[{not json")
    assert load_prices(p) is None


def test_load_prices_not_a_list(tmp_path) -> None:
    p = tmp_path / "prices.json"
    _write(p, {"model": "glm-5.3"})
    assert load_prices(p) is None


def test_load_prices_element_not_dict(tmp_path) -> None:
    p = tmp_path / "prices.json"
    _write(p, ["glm-5.3"])
    assert load_prices(p) is None


@pytest.mark.parametrize("entry", [{"in": 1, "per": 1}, {"model": 5, "per": 1}])
def test_load_prices_no_model_string(tmp_path, entry) -> None:
    p = tmp_path / "prices.json"
    _write(p, [entry])
    assert load_prices(p) is None


@pytest.mark.parametrize(
    "per",
    [
        pytest.param(None, id="missing"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param("1k", id="non-numeric"),
        pytest.param(True, id="bool"),
    ],
)
def test_load_prices_bad_per(tmp_path, per) -> None:
    p = tmp_path / "prices.json"
    entry = {"model": "glm-5.3", "in": 1, "out": 2, "cache": 3}
    if per is not None:
        entry["per"] = per
    _write(p, [entry])
    assert load_prices(p) is None


@pytest.mark.parametrize("field", ["in", "out", "cache"])
def test_load_prices_non_numeric_price(tmp_path, field) -> None:
    p = tmp_path / "prices.json"
    _write(p, [{"model": "glm-5.3", "per": 1, field: "free"}])
    assert load_prices(p) is None


def test_load_prices_valid(tmp_path) -> None:
    p = tmp_path / "prices.json"
    _write(
        p,
        [
            {"model": "glm-5.3@api.z.ai", "in": 6.9, "out": 24, "cache": 1.7,
             "per": 10000, "units": "credits"},
            {"model": "kimi-k3", "in": 3, "out": 15, "cache": 0.3,
             "per": 1000000, "units": "$"},
        ],
    )
    assert load_prices(p) == {
        "glm-5.3@api.z.ai": {"in": 6.9, "out": 24.0, "cache": 1.7,
                             "per": 10000, "units": "credits"},
        "kimi-k3": {"in": 3.0, "out": 15.0, "cache": 0.3,
                    "per": 1000000, "units": "$"},
    }


def test_load_prices_partial_fields(tmp_path) -> None:
    # Missing in/out/cache default to 0; missing units defaults to "".
    p = tmp_path / "prices.json"
    _write(p, [{"model": "MiniMax-M3", "per": 1000}])
    assert load_prices(p) == {
        "MiniMax-M3": {"in": 0.0, "out": 0.0, "cache": 0.0,
                       "per": 1000, "units": ""},
    }


def test_load_prices_duplicate_keys_last_wins(tmp_path) -> None:
    p = tmp_path / "prices.json"
    _write(
        p,
        [
            {"model": "glm-5.3", "in": 1, "out": 1, "cache": 1, "per": 1,
             "units": "old"},
            {"model": "glm-5.3", "in": 2, "out": 4, "cache": 8, "per": 10,
             "units": "new"},
        ],
    )
    assert load_prices(p) == {
        "glm-5.3": {"in": 2.0, "out": 4.0, "cache": 8.0,
                    "per": 10, "units": "new"},
    }


def test_load_prices_empty_list(tmp_path) -> None:
    # A syntactically valid empty file behaves like "no prices": an empty
    # dict (falsy) rather than None — callers treat both as no-price.
    p = tmp_path / "prices.json"
    _write(p, [])
    assert load_prices(p) == {}


def test_prices_path_is_home_bound() -> None:
    # [decision] _PRICES_PATH is bound to HOME (not __file__) so the
    # subprocess integration tests get hermetic isolation via fake-HOME.
    assert _PRICES_PATH == _PRICES_PATH.home() / ".claude" / "status_line" / "prices.json"


# ---------------------------------------------------------------------------
# price_for
# ---------------------------------------------------------------------------

_PRICES = {
    "glm-5.3@api.z.ai": {"in": 6.9, "out": 24.0, "cache": 1.7,
                         "per": 10000, "units": "credits"},
    "glm-5.3": {"in": 1.0, "out": 2.0, "cache": 0.5, "per": 1000, "units": "$"},
    "kimi-k3": {"in": 3.0, "out": 15.0, "cache": 0.3,
                "per": 1000000, "units": "$"},
}


def test_price_for_prefers_host_key() -> None:
    assert price_for("glm-5.3", _PRICES, "api.z.ai") is _PRICES["glm-5.3@api.z.ai"]


def test_price_for_falls_back_to_plain_model() -> None:
    assert price_for("kimi-k3", _PRICES, "api.z.ai") is _PRICES["kimi-k3"]


def test_price_for_unknown_model() -> None:
    assert price_for("MiniMax-M3", _PRICES, "api.z.ai") is None


def test_price_for_empty_host_uses_plain_key() -> None:
    assert price_for("glm-5.3", _PRICES, "") is _PRICES["glm-5.3"]


def test_price_for_no_prices() -> None:
    assert price_for("glm-5.3", None, "api.z.ai") is None


# ---------------------------------------------------------------------------
# compute_cost
# ---------------------------------------------------------------------------

def test_compute_cost_exact_value() -> None:
    tokens = {"in": 10000, "out": 5000, "cached": 20000}
    price = {"in": 6.9, "out": 24.0, "cache": 1.7, "per": 10000,
             "units": "credits"}
    # (10000*6.9 + 5000*24 + 20000*1.7) / 10000 = 223000/10000 = 22.3
    assert compute_cost(tokens, price) == pytest.approx(22.3)


def test_compute_cost_zero_components() -> None:
    price = {"in": 3.0, "out": 15.0, "cache": 0.3, "per": 1000000,
             "units": "$"}
    assert compute_cost({"in": 0, "out": 0, "cached": 500000}, price) == pytest.approx(0.15)
    assert compute_cost({"in": 2000000, "out": 0, "cached": 0}, price) == pytest.approx(6.0)
    assert compute_cost({"in": 0, "out": 0, "cached": 0}, price) == 0.0


# ---------------------------------------------------------------------------
# format_cost
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # ≥ 1e6 → "X.XM"
        (1_000_000, "1.0M"),
        (1_500_000, "1.5M"),
        (2_345_678, "2.3M"),
        # 1000 ≤ v < 1e6 → "X.Xk"
        (1000, "1.0k"),
        (1500, "1.5k"),
        (40200, "40.2k"),
        (999_999, "1000.0k"),
        # 0.1 ≤ v < 1000 → 1 decimal, trailing ".0" stripped
        (402, "402"),
        (8.1, "8.1"),
        (8.0, "8"),
        (0.1, "0.1"),
        (42.04, "42"),
        (999.9, "999.9"),
        # < 0.1 → 2 decimals
        (0.04, "0.04"),
        (0.099, "0.10"),
        (0.0, "0.00"),
    ],
)
def test_format_cost_number(value: float, expected: str) -> None:
    assert format_cost(value, "") == expected


def test_format_cost_units_prefix() -> None:
    # units whose first char is not alnum → glued prefix ("$8.1")
    assert format_cost(8.1, "$") == "$8.1"
    assert format_cost(0.04, "₽") == "₽0.04"


def test_format_cost_units_suffix() -> None:
    # alnum units → " " suffix ("402 credits")
    assert format_cost(402, "credits") == "402 credits"
    assert format_cost(1_500_000, "CR") == "1.5M CR"


def test_format_cost_empty_units() -> None:
    assert format_cost(402, "") == "402"
    assert format_cost(0.04, "") == "0.04"
