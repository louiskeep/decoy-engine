"""Acceptance: l-diversity (Machanavajjhala et al. 2007), distinct, entropy and
recursive (c, l). Hand-computed fixtures."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from decoy_engine.quality.anonymity import LDiversityReport, measure_l_diversity


def _fixture() -> pd.DataFrame:
    """Three classes:
    - homogeneous: four rows, one disease (the homogeneity attack, section 3.1);
    - skewed: counts (6, 1, 1);
    - uniform: counts (2, 2, 2).
    """
    rows = [("h", "cancer")] * 4
    rows += [("s", "flu")] * 6 + [("s", "cold"), ("s", "asthma")]
    rows += [("u", "a")] * 2 + [("u", "b")] * 2 + [("u", "c")] * 2
    return pd.DataFrame(rows, columns=["q", "disease"])


SKEWED_ENTROPY_L = math.exp(-(0.75 * math.log(0.75) + 2 * 0.125 * math.log(0.125)))


def test_entropy_reference_value() -> None:
    assert pytest.approx(2.08678, abs=1e-5) == SKEWED_ENTROPY_L


def test_distinct_and_entropy() -> None:
    report = measure_l_diversity(_fixture(), ["q"], "disease")
    assert isinstance(report, LDiversityReport)
    assert report.row_count == 18
    assert report.class_count == 3
    assert report.distinct_l_min == 1
    assert report.entropy_l_min == pytest.approx(1.0, abs=1e-12)
    assert report.recursive_c is None
    assert report.recursive_l_min is None
    assert report.l_target is None
    assert report.distinct_meets_target is None
    assert report.entropy_meets_target is None
    assert report.recursive_meets_target is None


def test_entropy_thresholds_per_class() -> None:
    """Skewed class has entropy l 2.0868; the uniform class exactly 3."""
    frame = _fixture()
    without_homogeneous = frame[frame["q"] != "h"]
    report = measure_l_diversity(without_homogeneous, ["q"], "disease", l_target=3)
    assert report.entropy_l_min == pytest.approx(SKEWED_ENTROPY_L, abs=1e-12)
    # The uniform class (entropy exactly ln 3) meets l = 3 despite rounding.
    assert report.entropy_classes_below_target == 1
    assert report.entropy_rows_below_target == 8
    assert report.distinct_classes_below_target == 0
    assert report.distinct_meets_target is True
    uniform_only = frame[frame["q"] == "u"]
    assert (
        measure_l_diversity(uniform_only, ["q"], "disease", l_target=3).entropy_meets_target is True
    )


def test_recursive_at_c_2() -> None:
    """c = 2: homogeneous l=1 holds (4 < 2*4) -> 1; skewed l=2 needs
    6 < 2*(1+1), false -> 1; uniform l=3 needs 2 < 2*2, true -> 3."""
    report = measure_l_diversity(_fixture(), ["q"], "disease", l_target=2, recursive_c=2)
    assert report.recursive_c == 2.0
    assert report.recursive_l_min == 1
    assert report.distinct_classes_below_target == 1
    assert report.distinct_rows_below_target == 4
    assert report.entropy_classes_below_target == 1
    assert report.entropy_rows_below_target == 4
    assert report.recursive_classes_below_target == 2
    assert report.recursive_rows_below_target == 12
    assert report.recursive_meets_target is False
    uniform_only = _fixture()[lambda f: f["q"] == "u"]
    uniform = measure_l_diversity(uniform_only, ["q"], "disease", l_target=3, recursive_c=2)
    assert uniform.recursive_l_min == 3
    assert uniform.recursive_meets_target is True


def test_recursive_at_c_1_can_be_zero() -> None:
    """c = 1 on the homogeneous class: 4 < 1*4 is false, so recursive l is 0."""
    homogeneous = _fixture()[lambda f: f["q"] == "h"]
    assert measure_l_diversity(homogeneous, ["q"], "disease", recursive_c=1).recursive_l_min == 0


def test_recursive_needs_c() -> None:
    report = measure_l_diversity(_fixture(), ["q"], "disease", l_target=2)
    assert report.recursive_l_min is None
    assert report.recursive_classes_below_target is None
    assert report.recursive_rows_below_target is None
    assert report.recursive_meets_target is None
    assert report.distinct_meets_target is False


def test_null_sensitive_is_excluded() -> None:
    frame = pd.DataFrame({"q": ["a", "a", "b", "b"], "s": ["x", None, "y", "z"]})
    report = measure_l_diversity(frame, ["q"], "s")
    assert report.distinct_l_min == 1
    assert report.rows_with_null_sensitive == 1
    assert report.classes_without_sensitive_values == 0


def test_all_null_class_has_l_zero_and_fails() -> None:
    frame = pd.DataFrame({"q": ["a", "a", "b"], "s": ["x", "y", None]})
    report = measure_l_diversity(frame, ["q"], "s", l_target=1, recursive_c=3)
    assert report.classes_without_sensitive_values == 1
    assert report.distinct_l_min == 0
    assert report.entropy_l_min == 0.0
    assert report.recursive_l_min == 0
    assert report.distinct_meets_target is False
    assert report.entropy_meets_target is False
    assert report.recursive_meets_target is False
    assert report.distinct_rows_below_target == 1


def test_empty_table() -> None:
    frame = pd.DataFrame({"q": pd.Series([], dtype=object), "s": pd.Series([], dtype=object)})
    report = measure_l_diversity(frame, ["q"], "s", l_target=2, recursive_c=2)
    assert report.class_count == 0
    assert report.distinct_l_min == 0
    assert report.entropy_l_min == 0.0
    assert report.recursive_l_min == 0
    assert report.distinct_meets_target is None
    assert report.entropy_meets_target is None
    assert report.recursive_meets_target is None
