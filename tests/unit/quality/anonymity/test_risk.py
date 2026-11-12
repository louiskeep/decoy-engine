"""Hand-computed fixtures for the re-identification risk measure (ARX / El Emam formulas).

Notation: ``f`` released class sizes, ``n = sum f``, ``J`` classes; ``F`` the population size of
each released class; ``tau`` the threshold.
"""

from __future__ import annotations

import datetime
import math

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.quality.anonymity import (
    ReidentificationRiskReport,
    measure_k_anonymity,
    measure_reidentification_risk,
)


def _classes(sizes: dict[str, int], column: str = "g") -> pd.DataFrame:
    return pd.DataFrame({column: [label for label, size in sizes.items() for _ in range(size)]})


SAMPLE = _classes({"a": 2, "b": 3, "c": 5})
POPULATION = _classes({"a": 4, "b": 3, "c": 10, "z": 8})


def _risk(table: object, population: object = None, tau: float = 0.4) -> ReidentificationRiskReport:
    return measure_reidentification_risk(table, ["g"], risk_threshold=tau, population=population)


def test_prosecutor_figures() -> None:
    """f = 2, 3, 5; tau = 0.4: 1/2 > 0.4 only, so 2 rows at risk; highest 1/2; average 3/10."""
    report = _risk(SAMPLE)
    assert report.row_count == 10
    assert report.class_count == 3
    assert report.k_min == 2
    assert report.risk_threshold == 0.4
    assert report.rows_at_risk_prosecutor == 2
    assert report.records_at_risk_prosecutor == 0.2
    assert report.highest_prosecutor_risk == 0.5
    assert report.average_prosecutor_risk == 0.3


def test_no_population_gives_no_journalist_or_marketer_figure() -> None:
    report = _risk(SAMPLE)
    assert report.population_rows is None
    assert report.rows_at_risk_journalist is None
    assert report.records_at_risk_journalist is None
    assert report.highest_journalist_risk is None
    assert report.average_journalist_risk is None
    assert report.average_marketer_risk is None


def test_journalist_and_marketer_second_arm() -> None:
    """F = 4, 3, 10 (the z class is not released): highest 1/3; no 1/F > 0.4; average
    max(3/17, (2/4 + 3/3 + 5/10)/10) = max(0.176.., 0.2) = 0.2; marketer 0.2."""
    report = _risk(SAMPLE, POPULATION)
    assert report.population_rows == 25
    assert report.highest_journalist_risk == 1 / 3
    assert report.rows_at_risk_journalist == 0
    assert report.records_at_risk_journalist == 0.0
    assert report.average_journalist_risk == 0.2
    assert report.average_marketer_risk == 0.2


def test_journalist_first_arm_and_records_at_risk() -> None:
    """f = 1, 9; F = 2, 9 plus 20 rows in a class not released; tau = 0.4.
    J / sum F = 2/11 (2/31 if the absent class were counted) beats (1/2 + 9/9)/10 = 0.15.
    The f = 1 class has F = 2 and 1/2 > 0.4: one released row at risk (two if F rows counted).
    Prosecutor: 1/1 > 0.4, so one row at risk; highest 1.0; average 2/10."""
    sample = _classes({"a": 1, "b": 9})
    population = _classes({"a": 2, "b": 9, "z": 20})
    report = _risk(sample, population)
    assert report.average_journalist_risk == 2 / 11
    assert report.average_marketer_risk == 0.15
    assert report.highest_journalist_risk == 0.5
    assert report.rows_at_risk_journalist == 1
    assert report.records_at_risk_journalist == 0.1
    assert report.rows_at_risk_prosecutor == 1
    assert report.highest_prosecutor_risk == 1.0
    assert report.average_prosecutor_risk == 0.2


def test_threshold_is_strict() -> None:
    """Size 2 at tau = 0.5: 1/2 is not above 0.5."""
    assert _risk(_classes({"a": 2}), tau=0.5).rows_at_risk_prosecutor == 0


def test_threshold_is_read_as_its_decimal() -> None:
    """Size 3 at tau = 0.3333333333333333: 1/3 exceeds that decimal, although the float
    comparison 1/3 > 0.3333333333333333 is false."""
    assert not (1 / 3 > 0.3333333333333333)
    report = _risk(_classes({"a": 3}), tau=0.3333333333333333)
    assert report.rows_at_risk_prosecutor == 3


def test_threshold_one_puts_nothing_at_risk() -> None:
    report = _risk(_classes({"a": 1, "b": 1}), tau=1)
    assert report.rows_at_risk_prosecutor == 0
    assert report.risk_threshold == 1.0


def test_empty_table() -> None:
    report = _risk(SAMPLE.iloc[:0])
    assert report.row_count == 0
    assert report.class_count == 0
    assert report.k_min is None
    assert report.highest_prosecutor_risk is None
    assert report.average_prosecutor_risk is None
    assert report.rows_at_risk_prosecutor is None
    assert report.records_at_risk_prosecutor is None


def test_empty_table_with_population() -> None:
    report = _risk(SAMPLE.iloc[:0], POPULATION)
    assert report.population_rows == 25
    assert report.average_journalist_risk is None
    assert report.average_marketer_risk is None


def test_empty_population_is_refused_for_a_non_empty_release() -> None:
    with pytest.raises(ValueError, match="3 released classes"):
        _risk(SAMPLE, POPULATION.iloc[:0])


@pytest.mark.parametrize(
    "population",
    [
        _classes({"a": 4, "b": 3}),  # c missing
        _classes({"a": 1, "b": 3, "c": 10}),  # F < f for a
    ],
)
def test_population_must_be_consistent_with_a_superset(population: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match=r"^the population is not a superset of the release: 1 "):
        _risk(SAMPLE, population)


def test_type_families_must_match_int_against_widened_float() -> None:
    sample = pd.DataFrame({"g": [1, 1, 2]})
    population = pd.DataFrame({"g": [1.0, 1.0, 2.0, None]})
    with pytest.raises(TypeError, match="'g'") as caught:
        _risk(sample, population)
    assert "1.0" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_type_families_must_match_arrow_date_against_timestamp() -> None:
    days = [datetime.date(2020, 1, 1), datetime.date(2020, 1, 1)]
    sample = pa.table({"g": pa.array(days, type=pa.date32())})
    population = pd.DataFrame({"g": pd.to_datetime(["2020-01-01", "2020-01-01", "2020-01-02"])})
    with pytest.raises(TypeError, match="'g'"):
        _risk(sample, population)


def test_matching_types_group_across_arrow_and_pandas() -> None:
    sample = pa.table({"g": pa.array([1, 1, None], type=pa.int64())})
    population = pd.DataFrame({"g": pd.array([1, 1, 1, None, None, 7], dtype="Int64")})
    report = _risk(sample, population)
    # f = 2 (value 1), 1 (null); F = 3, 2.
    assert report.highest_journalist_risk == 0.5
    assert report.average_marketer_risk == math.fsum([2 / 3, 1 / 2]) / 3


def test_population_column_checks() -> None:
    with pytest.raises(ValueError, match="'g'"):
        _risk(SAMPLE, pd.DataFrame({"other": [1]}))
    with pytest.raises(TypeError):
        _risk(SAMPLE, [1, 2, 3])


@pytest.mark.parametrize("tau", [True, 0, 0.0, -0.1, 1.5, float("nan"), float("inf"), "0.2", None])
def test_threshold_refusals(tau: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _risk(SAMPLE, tau=tau)  # type: ignore[arg-type]


def test_prosecutor_figures_match_k_anonymity() -> None:
    k = measure_k_anonymity(SAMPLE, ["g"])
    report = _risk(SAMPLE)
    assert report.highest_prosecutor_risk == k.highest_prosecutor_risk
    assert report.average_prosecutor_risk == k.average_prosecutor_risk


def test_inputs_are_not_modified() -> None:
    sample, population = SAMPLE.copy(), POPULATION.copy()
    _risk(sample, population)
    pd.testing.assert_frame_equal(sample, SAMPLE)
    pd.testing.assert_frame_equal(population, POPULATION)
