"""Categorical CDF builder: exact integer arithmetic, interpreter-independent output.

`_build_cdf` used a float running sum over a builtin `sum()` total, and CPython 3.12
made `sum()` of floats compensated, so thresholds moved by one slot on 3.12 for about
2.3% of weight vectors. These tests pin the exact builder against an independent
`fractions.Fraction` oracle (`_cdf_oracle`) that the implementation does not use.

The pinned corpus digest is the cross-interpreter proof: the same constant must pass on
CPython 3.10, 3.11 and 3.12.
"""

from __future__ import annotations

import itertools
import re
from decimal import Decimal
from fractions import Fraction
from typing import Any

import numpy as np
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._categorical import _WEIGHTED_CDF_RES, _build_cdf
from tests.unit.execution._cdf_oracle import (
    BELOW_RESOLUTION,
    NEGATIVE,
    NONPOSITIVE,
    RES,
    Outcome,
    corpus,
    error_corpus,
    oracle,
    outcome_digest,
)

# sha256 of the oracle's outcomes over `corpus()`; identical on 3.10, 3.11 and 3.12.
CORPUS_DIGEST = "d04e9f0a132605efaf7c6ef2935bc0d62ef483909b131ab037f14886803699a3"

_INDEX = re.compile(r"index (\d+)")


def _built(weights: list[Any]) -> Outcome:
    try:
        return ("ok", tuple(_build_cdf(weights)))
    except StrategyError as exc:
        match = _INDEX.search(exc.message)
        return ("err", str(exc.code), int(match.group(1)) if match else None)


def _refusal(weights: list[Any]) -> StrategyError:
    with pytest.raises(StrategyError) as caught:
        _build_cdf(weights)
    return caught.value


def test_resolution_constant_matches_the_oracle() -> None:
    assert _WEIGHTED_CDF_RES == RES


# ── T1 known answers ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("weights", "expected"),
    [
        ([0.6, 0.3, 0.1], [600_000, 900_000, 1_000_000]),
        ([0.1] * 10, [100_000 * k for k in range(1, 11)]),
        ([1.0, 1.0, 1.0], [333_333, 666_667, 1_000_000]),
        ([1, 1, 1], [333_333, 666_667, 1_000_000]),
        ([1.0, 0.0, 1.0], [500_000, 500_000, 1_000_000]),
        ([1.0, -0.0, 1.0], [500_000, 500_000, 1_000_000]),
        ([5, 1, 2, 0.5, 8], [303_030, 363_636, 484_848, 515_152, 1_000_000]),
        ([4503609762803756, 13510822533, 9007199259373711], [333_334, 333_335, 1_000_000]),
        ([np.float32(0.5), np.float16(0.25), np.int64(1)], [285_714, 428_571, 1_000_000]),
    ],
    ids=["skewed", "tenths", "ties-float", "ties-int", "zero", "neg-zero", "d1b2", "big-int", "np"],
)
def test_known_answers(weights: list[Any], expected: list[int]) -> None:
    assert _build_cdf(weights) == expected
    assert oracle(weights) == ("ok", tuple(expected))


def test_overflowing_total_is_refused_as_nonpositive() -> None:
    """Exact shares of [1e308, 1e308] are 1/2 each, so the zero-width guard cannot fire;
    the exact total exceeds the largest double and is refused before thresholds."""
    exc = _refusal([1e308, 1e308])
    assert exc.code == NONPOSITIVE
    assert exc.message == (
        "strategy='categorical': categorical weights sum above the largest finite double; "
        "cannot normalize."
    )


def test_tiny_weight_is_refused_at_its_index() -> None:
    exc = _refusal([1.0, 1e-20, 1.0])
    assert exc.code == BELOW_RESOLUTION
    assert exc.message == (
        "strategy='categorical': categorical weight at index 1 is below the CDF "
        "resolution (1 / 1000000 of total). The category would never be selected; "
        "remove the category or raise its weight."
    )


@pytest.mark.parametrize(
    ("bad", "code", "text"),
    [
        (float("nan"), NONPOSITIVE, "is NaN or infinite"),
        (float("inf"), NONPOSITIVE, "is NaN or infinite"),
        (float("-inf"), NONPOSITIVE, "is NaN or infinite"),
        (np.float32("nan"), NONPOSITIVE, "is NaN or infinite"),
        (True, NONPOSITIVE, "is not a number"),
        (np.bool_(True), NONPOSITIVE, "is not a number"),
        ("x", NONPOSITIVE, "is not a number"),
        (None, NONPOSITIVE, "is not a number"),
        (Decimal("1"), NONPOSITIVE, "is not a number"),
        (Fraction(1, 3), NONPOSITIVE, "is not a number"),
        (np.longdouble(1), NONPOSITIVE, "is not a number"),
        (-0.5, NEGATIVE, "is negative"),
        (np.int64(-1), NEGATIVE, "is negative"),
    ],
    ids=[
        "nan",
        "inf",
        "-inf",
        "f32-nan",
        "bool",
        "np-bool",
        "str",
        "none",
        "decimal",
        "fraction",
        "longdouble",
        "neg-float",
        "neg-int64",
    ],
)
def test_refusals_report_the_index_only(bad: Any, code: str, text: str) -> None:
    exc = _refusal([bad, 1.0])
    assert exc.code == code
    assert exc.message.startswith(f"strategy='categorical': categorical weight at index 0 {text}")
    assert oracle([bad, 1.0]) == ("err", code, 0)


def test_all_zero_is_nonpositive() -> None:
    exc = _refusal([0.0, -0.0, 0])
    assert exc.code == NONPOSITIVE
    assert exc.message == "strategy='categorical': categorical weights sum to 0; cannot normalize."


@pytest.mark.parametrize(
    ("weights", "expected"),
    [
        # Negative is a whole-vector pass before the total, whatever the float sum says.
        ([1e16, 1.0, -1e16], ("err", NEGATIVE, 2)),
        ([-1.0, 0.0], ("err", NEGATIVE, 0)),
        # Non-finite before negative; type before non-finite.
        ([float("nan"), -1], ("err", NONPOSITIVE, 0)),
        ([-1.0, float("inf")], ("err", NONPOSITIVE, 1)),
        ([float("nan"), "x"], ("err", NONPOSITIVE, 1)),
        ([True, -1.0], ("err", NONPOSITIVE, 0)),
        ([1e-9, -1.0], ("err", NEGATIVE, 1)),
    ],
)
def test_pass_order(weights: list[Any], expected: Outcome) -> None:
    assert _built(weights) == expected
    assert oracle(weights) == expected


def test_one_ulp_scaling_by_powers_of_two_is_invariant() -> None:
    base = [0.3, 0.1, 0.6, 1e-3]
    assert _build_cdf(base) == _build_cdf([w * 2.0**40 for w in base])
    assert _build_cdf(base) == _build_cdf([w * 2.0**-40 for w in base])


# ── T2 oracle parity ─────────────────────────────────────────────


def _small_vectors() -> list[list[Any]]:
    out: list[list[Any]] = []
    for n in range(1, 5):
        for combo in itertools.product(range(7), repeat=n):
            out.append(list(combo))
            out.append([float(x) for x in combo])
    return out


def test_exhaustive_small_integer_vectors_match_the_oracle() -> None:
    vectors = _small_vectors()
    assert len(vectors) == 2 * (7 + 49 + 343 + 2401)
    mismatches = [v for v in vectors if _built(v) != oracle(v)]
    assert mismatches == []


@pytest.fixture(scope="module")
def corpus_outcomes() -> tuple[list[list[float]], list[Outcome]]:
    vectors = corpus()
    return vectors, [oracle(v) for v in vectors]


def test_corpus_matches_the_oracle(
    corpus_outcomes: tuple[list[list[float]], list[Outcome]],
) -> None:
    vectors, expected = corpus_outcomes
    assert len(vectors) == 20_000
    mismatches = [i for i, v in enumerate(vectors) if _built(v) != expected[i]]
    assert mismatches == []


def test_error_corpus_matches_the_oracle_code_and_index() -> None:
    vectors = error_corpus()
    outcomes = [oracle(v) for v in vectors]
    assert sum(1 for o in outcomes if o[0] == "err") > 2_500  # the corpus is mostly refusals
    assert {o[1] for o in outcomes if o[0] == "err"} == {NONPOSITIVE, NEGATIVE, BELOW_RESOLUTION}
    mismatches = [i for i, v in enumerate(vectors) if _built(v) != outcomes[i]]
    assert mismatches == []


def test_mixed_numeric_types_match_the_oracle() -> None:
    rng = np.random.default_rng(11)
    makers = [float, np.float64, np.float32, np.float16, int, np.int64, np.uint32]
    mismatches = []
    for k in range(2_000):
        size = int(rng.integers(2, 30))
        vec = [makers[int(rng.integers(0, len(makers)))](rng.integers(0, 50)) for _ in range(size)]
        if k % 3 == 0:
            vec[0] = 2**53 + 1
        if _built(vec) != oracle(vec):
            mismatches.append(vec)
    assert mismatches == []


# ── T3 cross-interpreter digest ──────────────────────────────────


def test_corpus_digest_is_pinned(
    corpus_outcomes: tuple[list[list[float]], list[Outcome]],
) -> None:
    """Run on 3.10, 3.11 and 3.12: the same constant must pass on each."""
    vectors, expected = corpus_outcomes
    assert outcome_digest(expected) == CORPUS_DIGEST
    assert outcome_digest([_built(v) for v in vectors]) == CORPUS_DIGEST


# ── T6 value-free refusals ───────────────────────────────────────

_CANARY = "1234.5678"


class _LyingFloat(float):
    def __repr__(self) -> str:
        return _CANARY

    __str__ = __repr__

    def __float__(self) -> float:
        return 1234.5678

    def as_integer_ratio(self) -> tuple[int, int]:
        raise AssertionError("the builder must not call a method on the weight")


_VALUE_FREE_CASES = [
    [1234.5678, -1234.5678],
    [1234.5678, 1e-20 * 1234.5678, 1234.5678],
    [1234.5678, float("nan")],
    [_LyingFloat(1234.5678), 1234.5678],
    [1234.5678, "1234.5678"],
    [0.0 * 1234.5678, 0.0],
    [1e308, 1e308, 1234.5678],
]


@pytest.mark.parametrize("weights", _VALUE_FREE_CASES)
def test_refusals_carry_no_value_type_name_or_chained_exception(weights: list[Any]) -> None:
    exc = _refusal(weights)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    text = f"{exc} {exc.message} {exc.args!r}"
    for forbidden in (_CANARY, "1234", "LyingFloat", "float", "<class", "e+308", "e-20", "Decimal"):
        assert forbidden not in text, forbidden
