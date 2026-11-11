"""Pin the distinct-value weighting and confidence buckets in detection/_scoring."""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from decoy_engine.detection._scoring import (
    HIGH_CONFIDENCE_NO_HINT_FLOOR,
    HIGH_CONFIDENCE_WITH_HINT_FLOOR,
    NAME_HINT_MIN_RATE,
    PreparedValues,
    _confidence_bucket,
    _dominant_variant,
    _evaluate,
    _series_str,
    prepared_scope,
)

_DIGITS = re.compile(r"\d+")


def _prep(values: list[object]) -> PreparedValues:
    return PreparedValues(pd.Series(values, dtype=object))


def test_prepared_values_hold_uniques_and_counts_per_cell() -> None:
    p = _prep(["a", " a ", None, "b", "a"])
    assert p.n == 4
    assert p.counts.sum() == 4
    assert p.raw_uniques.tolist() == ["a", " a ", "b"]
    assert p.uniques.tolist() == ["a", "a", "b"]
    assert p.counts.tolist() == [2, 1, 1]
    assert p.length_stats() == (1, 3, 1.5)


def test_evaluate_weights_each_distinct_value_by_its_count() -> None:
    p = _prep(["1"] * 7 + ["x"] * 3)
    m = _evaluate("d", p, _DIGITS, name_hint=False, min_rate=0.5)
    assert m is not None
    assert m.match_rate == 0.7
    assert m.sample_misses == ["x", "x", "x"]
    assert m.confidence == "medium"


def test_evaluate_rate_below_threshold_is_none_and_at_threshold_fires() -> None:
    p = _prep(["1"] * 5 + ["x"] * 5)
    assert _evaluate("d", p, _DIGITS, name_hint=False, min_rate=0.5) is not None
    assert _evaluate("d", p, _DIGITS, name_hint=False, min_rate=0.51) is None
    assert _evaluate("d", _prep([None, None]), _DIGITS, name_hint=False, min_rate=0.0) is None


def test_evaluate_name_hint_lowers_the_threshold() -> None:
    n = 100
    hits = int(NAME_HINT_MIN_RATE * n) + 1
    p = _prep(["1"] * hits + ["x"] * (n - hits))
    assert _evaluate("d", p, _DIGITS, name_hint=False, min_rate=0.9) is None
    assert _evaluate("d", p, _DIGITS, name_hint=True, min_rate=0.9) is not None


def test_evaluate_sample_misses_are_the_first_three_in_row_order() -> None:
    p = _prep(["m1", "1", "m2", "m2", "m3", "m4", "1"])
    m = _evaluate("d", p, _DIGITS, name_hint=False, min_rate=0.0)
    assert m is not None
    assert m.sample_misses == ["m1", "m2", "m2"]


def test_evaluate_validator_error_skips_only_that_value() -> None:
    def validator(v: str) -> bool:
        if v == "1":
            raise ValueError("bad")
        return True

    p = _prep(["1", "2", "3", "4"])
    m = _evaluate("d", p, _DIGITS, name_hint=False, min_rate=0.0, validator=validator)
    assert m is not None
    assert m.match_rate == 0.75
    assert m.sample_misses == ["1"]


def test_evaluate_validator_false_is_a_miss() -> None:
    p = _prep(["1", "2", "2", "x"])
    m = _evaluate("d", p, _DIGITS, name_hint=False, min_rate=0.0, validator=lambda v: v == "2")
    assert m is not None
    assert m.match_rate == 0.5


@pytest.mark.parametrize(
    ("rate", "hint", "bucket"),
    [
        (HIGH_CONFIDENCE_NO_HINT_FLOOR, False, "high"),
        (HIGH_CONFIDENCE_NO_HINT_FLOOR - 0.01, False, "medium"),
        (0.50, False, "medium"),
        (0.49, False, "low"),
        (HIGH_CONFIDENCE_WITH_HINT_FLOOR, True, "high"),
        (HIGH_CONFIDENCE_WITH_HINT_FLOOR - 0.01, True, "medium"),
        (NAME_HINT_MIN_RATE, True, "medium"),
        (NAME_HINT_MIN_RATE - 0.01, True, "low"),
        (HIGH_CONFIDENCE_WITH_HINT_FLOOR, False, "low"),
        (0.50, True, "high"),
    ],
)
def test_confidence_bucket_boundaries(rate: float, hint: bool, bucket: str) -> None:
    assert _confidence_bucket(rate, name_hint=hint) == bucket


def test_dominant_variant_sums_counts_across_repeated_labels() -> None:
    uniques = pd.Series(["x1", "y1", "z1"])
    variants = [("A", re.compile(r"x\d")), ("A", re.compile(r"y\d")), ("B", re.compile(r"z\d"))]
    assert _dominant_variant(uniques, np.array([3, 2, 4]), variants) == "A"
    assert _dominant_variant(uniques, np.array([3, 2, 6]), variants) == "B"


def test_dominant_variant_is_none_when_nothing_matches() -> None:
    uniques = pd.Series(["q"])
    assert _dominant_variant(uniques, np.array([2]), [("A", re.compile(r"\d"))]) is None
    assert _dominant_variant(pd.Series([], dtype=object), np.array([]), []) is None


def test_dominant_variant_counts_a_value_under_its_first_variant_only() -> None:
    uniques = pd.Series(["1", "ab"])
    variants = [("any", re.compile(r".+")), ("digit", re.compile(r"\d"))]
    assert _dominant_variant(uniques, np.array([5, 1]), variants) == "any"


def test_series_str_reuses_the_scoped_preparation_only_for_that_series() -> None:
    s = pd.Series(["a", "b"])
    other = pd.Series(["c"])
    with prepared_scope(s):
        first = _series_str(s)
        assert _series_str(s) is first
        assert _series_str(other) is not first
        assert _series_str(other).uniques.tolist() == ["c"]
    assert _series_str(s) is not first
