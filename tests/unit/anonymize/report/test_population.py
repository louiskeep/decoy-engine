"""Journalist and marketer figures exist only with a supplied population."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from decoy_engine.anonymize.report import (
    RiskUtilityReport,
    RiskUtilityReportRead,
    build_risk_utility_report,
)
from decoy_engine.quality.anonymity import KAnonymity

from ._helpers import release, table

NO_POPULATION_ASSUMPTIONS = ["prosecutor_target_known_in_release", "suppressed_rows_not_released"]
POPULATION_ASSUMPTIONS = [
    "prosecutor_target_known_in_release",
    "population_consistent_with_superset",
    "suppressed_rows_not_released",
]
NOT_ESTABLISHED = [
    "expert_determination",
    "linkage_with_external_data",
    "population_risk_without_population",
    "suppressed_rows_risk",
]


def _payload(population: Any = None) -> dict[str, Any]:
    lattice, result, released = release(table(), (KAnonymity(2),))
    report = build_risk_utility_report(
        released, lattice, result, risk_threshold=0.2, population=population
    )
    return report.to_payload()


def test_without_a_population() -> None:
    payload = _payload()
    risk = payload["risk"]
    assert risk["population"] == "not_supplied"
    assert risk["population_rows"] is None
    assert risk["journalist"] is None
    assert risk["marketer"] is None
    assert risk["prosecutor"] is not None
    assert payload["assumptions"] == NO_POPULATION_ASSUMPTIONS
    assert payload["not_established"] == NOT_ESTABLISHED


def test_with_a_population() -> None:
    frame = table()
    payload = _payload(pd.concat([frame, frame], ignore_index=True))
    risk = payload["risk"]
    assert risk["population"] == "supplied"
    assert risk["population_rows"] == 2 * len(frame)
    assert set(risk["journalist"]) == {"highest", "average", "records_at_risk", "rows_at_risk"}
    assert set(risk["marketer"]) == {"average"}
    assert payload["assumptions"] == POPULATION_ASSUMPTIONS
    assert payload["not_established"] == [t for t in NOT_ESTABLISHED if "population" not in t]


def test_a_subset_released_against_its_full_table() -> None:
    """Journalist <= prosecutor (highest and average) and marketer <= journalist average."""
    full = pd.concat([table()] * 5, ignore_index=True)
    full["age"] = full["age"] + (full.index % 3)
    sample = full.sample(frac=0.4, random_state=7).reset_index(drop=True)
    lattice, result, released = release(sample, (KAnonymity(2),), 0.2)
    risk = build_risk_utility_report(
        released, lattice, result, risk_threshold=0.2, population=full
    ).to_payload()["risk"]
    prosecutor, journalist = risk["prosecutor"], risk["journalist"]
    assert journalist["highest"] <= prosecutor["highest"]
    assert journalist["average"] <= prosecutor["average"]
    assert risk["marketer"]["average"] <= journalist["average"]


@pytest.mark.parametrize("model", [RiskUtilityReport, RiskUtilityReportRead])
@pytest.mark.parametrize("section", ["journalist", "marketer"])
def test_writer_and_reader_refuse_figures_without_a_population(model: Any, section: str) -> None:
    frame = table()
    with_population = _payload(pd.concat([frame, frame], ignore_index=True))
    forged = _payload()
    forged["risk"][section] = with_population["risk"][section]
    with pytest.raises(ValidationError):
        model.model_validate(forged)


@pytest.mark.parametrize("model", [RiskUtilityReport, RiskUtilityReportRead])
def test_population_rows_follow_the_population_token(model: Any) -> None:
    forged = _payload()
    forged["risk"]["population_rows"] = 5
    with pytest.raises(ValidationError):
        model.model_validate(forged)
