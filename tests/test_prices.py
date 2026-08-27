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
    _is_num,
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


def test_provider_host_scheme_less_value_yields_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A scheme-less value has no netloc for urlparse, so hostname is None
    # → "" — "@host" price keys never match, plain keys still do. Pinned so
    # the silent disable is a documented outcome, not a surprise.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "api.z.ai")
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
    _write(p, "[{not json")
    assert load_prices(p) is None


def test_load_prices_utf8_bom_still_loads(tmp_path) -> None:
    # Windows editors save "UTF-8 with BOM"; a leading BOM must not break
    # the JSON parse (which would silently disable the cost columns).
    p = tmp_path / "prices.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        [{"model": "glm-5.3", "per": 1000}]
    ).encode("utf-8"))
    assert load_prices(p) == {
        "glm-5.3": {"in": 0.0, "out": 0.0, "cache": 0.0,
                    "per": 1000, "units": ""},
    }


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"model": "m", "per": float("nan")}, id="per-nan"),
        pytest.param({"model": "m", "per": float("inf")}, id="per-inf"),
        pytest.param(
            {"model": "m", "per": 1, "in": float("nan")}, id="in-nan"
        ),
        pytest.param(
            {"model": "m", "per": 1, "out": float("-inf")}, id="out-inf"
        ),
        pytest.param(
            {"model": "m", "per": 1, "cache": float("nan")}, id="cache-nan"
        ),
    ],
)
def test_load_prices_rejects_non_finite_numbers(tmp_path, entry) -> None:
    # json.loads accepts the NaN/Infinity extensions, so _is_num must
    # reject them explicitly — a NaN `per` (NaN <= 0 is False) would
    # otherwise flow into format_cost and render "$nan".
    p = tmp_path / "prices.json"
    _write(p, [entry])
    assert load_prices(p) is None


@pytest.mark.parametrize(
    "per", [float("nan"), float("inf"), float("-inf")]
)
def test_is_num_rejects_non_finite(per: float) -> None:
    assert not _is_num(per)


def test_load_prices_negative_price_accepted(tmp_path) -> None:
    # [pinned decision] The plan only requires prices to be NUMERIC, so a
    # negative price is accepted and flows into compute_cost/format_cost
    # as-is ("$-3.0"). Rejecting it would be a spec change; documented by
    # pinning rather than silently changed.
    p = tmp_path / "prices.json"
    _write(p, [{"model": "m", "per": 1, "in": -3, "out": 0, "cache": 0}])
    assert load_prices(p) == {
        "m": {"in": -3.0, "out": 0.0, "cache": 0.0, "per": 1, "units": ""},
    }
    # -3.0 < 0.1 → the two-decimal bucket → "$-3.00".
    assert format_cost(compute_cost({"in": 1, "out": 0, "cached": 0},
                                    {"in": -3.0, "per": 1}), "$") == "$-3.00"


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
        pytest.param("1K", id="non-numeric"),
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

# Named for its distinguishing feature: unlike the two-key _PRICES in
# test_render_output.py, this one also carries the PLAIN "glm-5.3" key
# the fallback-chain tests need (same-named constants with different
# contents in sibling files would be a drift trap).
_PRICES_WITH_PLAIN_FALLBACK = {
    "glm-5.3@api.z.ai": {"in": 6.9, "out": 24.0, "cache": 1.7,
                         "per": 10000, "units": "credits"},
    "glm-5.3": {"in": 1.0, "out": 2.0, "cache": 0.5, "per": 1000, "units": "$"},
    "kimi-k3": {"in": 3.0, "out": 15.0, "cache": 0.3,
                "per": 1000000, "units": "$"},
}


def test_price_for_prefers_host_key() -> None:
    assert (
        price_for("glm-5.3", _PRICES_WITH_PLAIN_FALLBACK, "api.z.ai")
        is _PRICES_WITH_PLAIN_FALLBACK["glm-5.3@api.z.ai"]
    )


def test_price_for_falls_back_to_plain_model() -> None:
    assert (
        price_for("kimi-k3", _PRICES_WITH_PLAIN_FALLBACK, "api.z.ai")
        is _PRICES_WITH_PLAIN_FALLBACK["kimi-k3"]
    )


def test_price_for_unknown_model() -> None:
    assert price_for("MiniMax-M3", _PRICES_WITH_PLAIN_FALLBACK, "api.z.ai") is None


def test_price_for_empty_host_uses_plain_key() -> None:
    assert (
        price_for("glm-5.3", _PRICES_WITH_PLAIN_FALLBACK, "")
        is _PRICES_WITH_PLAIN_FALLBACK["glm-5.3"]
    )


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
        # 1000 ≤ v < 1e6 → "X.XK"
        (1000, "1.0K"),
        (1500, "1.5K"),
        (40200, "40.2K"),
        (999_999, "1000.0K"),
        # 0.1 ≤ v < 1000 → 1 decimal, trailing ".0" stripped
        (402, "402"),
        (8.1, "8.1"),
        (8.0, "8"),
        (0.1, "0.1"),
        (42.04, "42"),
        (999.9, "999.9"),
        # bucket boundary: 999.96 rounds to 3 digits and the ".0" strip
        # leaves "1000" — no k suffix (the value entered < 1000)
        (999.96, "1000"),
        (999.94, "999.9"),
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
