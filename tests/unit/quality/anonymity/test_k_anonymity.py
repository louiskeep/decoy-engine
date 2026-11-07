"""Acceptance: k-anonymity (Sweeney 2002) and ARX's prosecutor risk figures."""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.quality.anonymity import KAnonymityReport, measure_k_anonymity


def _sized_classes() -> pd.DataFrame:
    """Ten rows in classes of sizes 1, 2, 3 and 4."""
    keys = ["a"] + ["b"] * 2 + ["c"] * 3 + ["d"] * 4
    return pd.DataFrame({"zip": keys, "sex": ["F"] * 10, "other": range(10)})


def test_class_sizes_and_risks() -> None:
    report = measure_k_anonymity(_sized_classes(), ["zip", "sex"])
    assert isinstance(report, KAnonymityReport)
    assert report.row_count == 10
    assert report.class_count == 4
    assert report.k_min == 1
    assert report.class_size_distribution == ((1, 1), (2, 1), (3, 1), (4, 1))
    assert report.rows_with_null_quasi_identifier == 0
    assert report.highest_prosecutor_risk == 1.0
    assert report.average_prosecutor_risk == pytest.approx(0.4)
    assert report.k_target is None
    assert report.meets_target is None
    assert report.classes_below_target is None
    assert report.rows_below_target is None


def test_target_counts() -> None:
    report = measure_k_anonymity(_sized_classes(), ["zip", "sex"], k_target=3)
    assert report.classes_below_target == 2
    assert report.rows_below_target == 3
    assert report.meets_target is False
    assert measure_k_anonymity(_sized_classes(), ["zip"], k_target=1).meets_target is True


def test_combined_quasi_identifiers_split_classes() -> None:
    frame = pd.DataFrame({"a": [1, 1, 2, 2], "b": ["x", "y", "x", "x"]})
    report = measure_k_anonymity(frame, ["a", "b"])
    assert report.class_size_distribution == ((1, 2), (2, 1))
    assert report.k_min == 1
    assert measure_k_anonymity(frame, ["a"]).k_min == 2


def test_empty_table() -> None:
    frame = pd.DataFrame({"zip": pd.Series([], dtype=object)})
    report = measure_k_anonymity(frame, ["zip"], k_target=2)
    assert report.row_count == 0
    assert report.class_count == 0
    assert report.k_min is None
    assert report.class_size_distribution == ()
    assert report.highest_prosecutor_risk is None
    assert report.average_prosecutor_risk is None
    assert report.meets_target is None
    assert report.classes_below_target == 0
    assert report.rows_below_target == 0


def test_row_order_does_not_matter() -> None:
    frame = _sized_classes()
    shuffled = frame.sample(frac=1.0, random_state=7).reset_index(drop=True)
    assert measure_k_anonymity(frame, ["zip"], k_target=2) == measure_k_anonymity(
        shuffled, ["zip"], k_target=2
    )
