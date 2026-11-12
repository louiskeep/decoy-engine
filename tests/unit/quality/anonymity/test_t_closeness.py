"""Acceptance: t-closeness (Li, Li and Venkatasubramanian, ICDE 2007).

The salary fixtures are the paper's published releases (Tables 4 and 5): nine
rows, salaries 3k..11k once each, so the table distribution Q is uniform over
m = 9 values and the ordered-distance denominator is m - 1 = 8.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.quality.anonymity import (
    TClosenessReport,
    measure_t_closeness_equal,
    measure_t_closeness_ordered,
)

TOL = 1e-12


def _release(classes: list[tuple[tuple[str, str], list[int], list[str]]]) -> pd.DataFrame:
    rows = []
    for (zip_code, age), salaries, diseases in classes:
        for salary, disease in zip(salaries, diseases, strict=True):
            rows.append({"zip": zip_code, "age": age, "salary": salary, "disease": disease})
    return pd.DataFrame(rows)


TABLE_4 = _release(
    [
        (("476**", "2*"), [3, 4, 5], ["gastric ulcer", "gastritis", "stomach cancer"]),
        (("4790*", ">=40"), [6, 11, 8], ["gastritis", "flu", "bronchitis"]),
        (("476**", "3*"), [7, 9, 10], ["bronchitis", "pneumonia", "stomach cancer"]),
    ]
)

TABLE_5 = _release(
    [
        (("4767*", "<=40"), [3, 5, 9], ["gastric ulcer", "stomach cancer", "pneumonia"]),
        (("4790*", ">=40"), [6, 11, 8], ["gastritis", "flu", "bronchitis"]),
        (("4760*", "<=40"), [4, 7, 10], ["gastritis", "bronchitis", "stomach cancer"]),
    ]
)

QI = ["zip", "age"]


def test_table_4_ordered_emd_matches_paper() -> None:
    """Class {3k,4k,5k}: r = (2,2,2,-1,-1,-1,-1,-1,-1)/9, cumulative
    (2,4,6,5,4,3,2,1,0)/9, sum 27/9 = 3, EMD 3/8 = 0.375 (the paper's figure).
    {6k,8k,11k}: cumulative |.| sum 12/9, EMD 1/6. {7k,9k,10k}: 17/9, EMD 17/72."""
    report = measure_t_closeness_ordered(TABLE_4, QI, "salary")
    assert report.ground_distance == "ordered"
    assert report.distinct_sensitive_values == 9
    assert report.classes_measured == 3
    assert report.max_emd == pytest.approx(0.375, abs=TOL)


def test_table_4_each_class_value() -> None:
    """Each class's EMD is pinned by thresholds just either side of it:
    3/8 > 17/72 > 1/6."""
    for t, above in (
        (3 / 8, 0),
        (3 / 8 - 1e-9, 1),
        (17 / 72, 1),
        (17 / 72 - 1e-9, 2),
        (1 / 6, 2),
        (1 / 6 - 1e-9, 3),
    ):
        report = measure_t_closeness_ordered(TABLE_4, QI, "salary", t_target=t)
        assert report.classes_above_target == above, t
        assert report.rows_above_target == 3 * above


def test_table_5_ordered_emd_matches_paper() -> None:
    """Classes {3k,5k,9k}, {6k,8k,11k}, {4k,7k,10k}: 1/6, 1/6, 1/12. The paper
    reports Table 5 as 0.167-close with respect to Salary."""
    report = measure_t_closeness_ordered(TABLE_5, QI, "salary", t_target=0.17)
    assert report.max_emd == pytest.approx(1 / 6, abs=TOL)
    assert report.meets_target is True
    assert report.classes_above_target == 0
    failing = measure_t_closeness_ordered(TABLE_5, QI, "salary", t_target=0.16)
    assert failing.meets_target is False
    assert failing.classes_above_target == 2
    assert failing.rows_above_target == 6


def test_table_5_equal_emd_hand_computed() -> None:
    """Hand-computed fixture on the Table 5 release (not a paper number; the
    paper's 0.278 uses hierarchical distance). Q: gastric ulcer 1/9,
    gastritis 2/9, stomach cancer 2/9, flu 1/9, bronchitis 2/9, pneumonia 1/9.
    Class {GU, SC, Pn}: 1/2 (2+1+2 + 2+1+2)/9 = 5/9.
    Class {Gi, flu, Br}: 1/2 (1+2+1 + 1+2+1)/9 = 4/9.
    Class {Gi, Br, SC}: 1/2 (1+1+1 + 1+1+1)/9 = 1/3."""
    report = measure_t_closeness_equal(TABLE_5, QI, "disease")
    assert report.ground_distance == "equal"
    assert report.max_emd == pytest.approx(5 / 9, abs=TOL)
    for t, above in ((0.56, 0), (0.5, 1), (0.4, 2), (0.3, 3)):
        r = measure_t_closeness_equal(TABLE_5, QI, "disease", t_target=t)
        assert r.classes_above_target == above, t
    # 4/9 and 1/3 exactly at the boundary count as meeting t.
    assert (
        measure_t_closeness_equal(TABLE_5, QI, "disease", t_target=4 / 9).classes_above_target == 1
    )
    assert (
        measure_t_closeness_equal(TABLE_5, QI, "disease", t_target=1 / 3).classes_above_target == 2
    )


def test_single_value_table_has_zero_ordered_emd() -> None:
    frame = pd.DataFrame({"q": ["a", "b"], "s": [5, 5]})
    report = measure_t_closeness_ordered(frame, ["q"], "s", t_target=0.0)
    assert report.max_emd == 0.0
    assert report.meets_target is True


def test_unmeasured_class_makes_t_undetermined() -> None:
    """A class with only null sensitive values cannot be measured, so the
    target is undetermined (None), never a silent pass."""
    frame = pd.DataFrame({"q": ["a", "a", "b", "b", "c"], "s": [1.0, 2.0, 1.0, 2.0, None]})
    for measure in (measure_t_closeness_equal, measure_t_closeness_ordered):
        report = measure(frame, ["q"], "s", t_target=0.5)
        assert report.max_emd == pytest.approx(0.0, abs=TOL)
        assert report.classes_without_sensitive_values == 1
        assert report.rows_in_unmeasured_classes == 1
        assert report.classes_measured == 2
        assert report.meets_target is None
    # A measured class above t decides the target: False.
    skewed = pd.DataFrame({"q": ["a", "a", "b", "b", "c"], "s": [1.0, 1.0, 2.0, 2.0, None]})
    for measure in (measure_t_closeness_equal, measure_t_closeness_ordered):
        report = measure(skewed, ["q"], "s", t_target=0.1)
        assert report.meets_target is False


def test_no_sensitive_values_at_all() -> None:
    frame = pd.DataFrame({"q": ["a", "b"], "s": [None, None]}, dtype=object)
    report = measure_t_closeness_equal(frame, ["q"], "s", t_target=0.2)
    assert report.max_emd is None
    assert report.meets_target is None
    assert report.classes_without_sensitive_values == 2


def test_no_target_means_no_verdict() -> None:
    report = measure_t_closeness_equal(TABLE_5, QI, "disease")
    assert report.t_target is None
    assert report.meets_target is None
    assert report.classes_above_target is None
    assert report.rows_above_target is None


def test_ordered_refuses_unordered_columns() -> None:
    for column in (
        pd.Series(["a", "b", "c"]),
        pd.Series(pd.Categorical(["a", "b", "c"], ordered=False)),
        pd.Series([1, "b", 2.0], dtype=object),
    ):
        frame = pd.DataFrame({"q": ["x", "x", "y"], "s": column})
        with pytest.raises(TypeError, match="'s'"):
            measure_t_closeness_ordered(frame, ["q"], "s")


def test_ordered_accepts_ordered_kinds() -> None:
    cat = pd.Categorical(
        ["lo", "hi", "mid"], categories=["lo", "mid", "hi", "unused"], ordered=True
    )
    frame = pd.DataFrame({"q": ["x", "x", "y"], "s": cat})
    report = measure_t_closeness_ordered(frame, ["q"], "s")
    # Observed order lo < mid < hi, m = 3 (the unused category is not counted).
    assert report.distinct_sensitive_values == 3
    # Class x {lo, hi}: P (1/2, 0, 1/2), Q (1/3, 1/3, 1/3); cumulative r: 1/6, -1/6, 0;
    # sum 1/3, EMD (1/3)/2 = 1/6. Class y {mid}: cumulative -1/3, 1/3, 0 -> 2/3 / 2 = 1/3.
    assert report.max_emd == pytest.approx(1 / 3, abs=TOL)
    dates = pd.DataFrame(
        {"q": ["x", "x", "y"], "s": pd.to_datetime(["2020-01-01", "2020-03-01", "2020-02-01"])}
    )
    assert measure_t_closeness_ordered(dates, ["q"], "s").max_emd == pytest.approx(1 / 3, abs=TOL)


def test_ordered_arrow_dictionary_and_decimal() -> None:
    from decimal import Decimal

    ordered = pa.DictionaryArray.from_arrays(
        pa.array([0, 2, 1]), pa.array(["lo", "mid", "hi"]), ordered=True
    )
    table = pa.table({"q": ["x", "x", "y"], "s": ordered})
    assert measure_t_closeness_ordered(table, ["q"], "s").max_emd == pytest.approx(1 / 3, abs=TOL)
    unordered = pa.table(
        {"q": ["x", "x", "y"], "s": pa.array(["lo", "hi", "mid"]).dictionary_encode()}
    )
    with pytest.raises(TypeError, match="'s'"):
        measure_t_closeness_ordered(unordered, ["q"], "s")
    decimals = pa.table(
        {"q": ["x", "x", "y"], "s": pa.array([Decimal("1.0"), Decimal("3.0"), Decimal("2.0")])}
    )
    floats = pd.DataFrame({"q": ["x", "x", "y"], "s": [1.0, 3.0, 2.0]})
    assert measure_t_closeness_ordered(decimals, ["q"], "s") == measure_t_closeness_ordered(
        floats, ["q"], "s"
    )


def test_report_type() -> None:
    assert isinstance(measure_t_closeness_equal(TABLE_5, QI, "disease"), TClosenessReport)
