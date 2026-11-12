"""Build the risk and utility report from a released table and the search result that chose it.

Pattern: ARX risk analysis of an anonymized output, documented as the HHS OCR Expert Determination guidance asks.
  The figures are ARX's sample risk summary (``quality.anonymity`` measures them);
  the document states the method, the quasi-identifiers, the threshold, the
  attacker assumptions and what it does not establish, as the OCR guidance
  ("Guidance Regarding Methods for De-identification of Protected Health
  Information", HHS OCR, November 2012, section 2) asks of a documented
  determination. The judgement that a risk is "very small" stays with the expert.

The release is verified, not trusted: the suppressed rows are recomputed from
``generalized_including_suppressed`` and must equal ``released.suppressed``; the
released rows are then derived from the two, and ``released.table`` must equal
them. Every risk figure is measured on the derived rows.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.anonymize.hierarchies import Lattice
from decoy_engine.anonymize.report._model import RISK_UTILITY_REPORT_SCHEMA, RiskUtilityReport
from decoy_engine.anonymize.search import Released, SearchResult
from decoy_engine.quality.anonymity import (
    EqualTCloseness,
    FrequencySet,
    OrderedTCloseness,
    PrivacyModel,
    ReidentificationRiskReport,
    check_risk_threshold,
    measure_reidentification_risk,
)
from decoy_engine.reasons import approve_name

_METHOD = "full_domain_generalization_with_record_suppression"
_NOT_ESTABLISHED = (
    "expert_determination",
    "linkage_with_external_data",
    "population_risk_without_population",
    "suppressed_rows_risk",
)


def _rows(table: pd.DataFrame | pa.Table) -> int:
    return len(table) if isinstance(table, pd.DataFrame) else table.num_rows


def _check_arguments(released: object, lattice: object, result: object, population: object) -> None:
    if not isinstance(released, Released):
        raise TypeError("released must be the Released returned by apply_search")
    if not isinstance(lattice, Lattice):
        raise TypeError("lattice must be an anonymize.hierarchies.Lattice")
    if not isinstance(result, SearchResult):
        raise TypeError("result must be a SearchResult")
    if population is not None and not isinstance(population, (pd.DataFrame, pa.Table)):
        raise TypeError("population must be a pandas.DataFrame, a pyarrow.Table or None")
    if not result.found or result.node is None:
        raise ValueError("the search found no node meeting the targets; there is no release")
    if lattice.columns != result.columns or lattice.heights != result.heights:
        raise ValueError("the result was computed on another lattice")
    names = [*lattice.columns, *(m.sensitive_column for m in result.models)]
    if any(name is not None and approve_name(name) is None for name in names):
        raise ValueError("a column name cannot be written to the report")


def _same_table(left: pd.DataFrame | pa.Table, right: pd.DataFrame | pa.Table) -> bool:
    if isinstance(left, pd.DataFrame) and isinstance(right, pd.DataFrame):
        return (
            left.columns.equals(right.columns)
            and left.index.equals(right.index)
            and left.dtypes.equals(right.dtypes)
            and left.equals(right)
        )
    if isinstance(left, pa.Table) and isinstance(right, pa.Table):
        return left.equals(right)
    return False


def _verified_release(released: Released, lattice: Lattice, result: SearchResult) -> Any:
    """The released rows derived from the verified suppression mask."""
    generalized = released.generalized_including_suppressed
    if not isinstance(generalized, (pd.DataFrame, pa.Table)) or _rows(generalized) != result.rows:
        raise ValueError("the release was computed on another table (row count differs)")
    mask = np.asarray(released.suppressed)
    if mask.dtype != np.bool_ or mask.shape != (result.rows,):
        raise ValueError("released.suppressed must be one bool per input row")
    frequencies = FrequencySet.of(generalized, lattice.columns, result.models)
    suppressed = frequencies.row_mask(frequencies.failing(result.models))
    if int(suppressed.sum()) != result.rows_suppressed or not np.array_equal(suppressed, mask):
        raise ValueError("the release's suppressed rows differ from the result's when recomputed")
    keep = ~suppressed
    if isinstance(generalized, pd.DataFrame):
        derived: pd.DataFrame | pa.Table = generalized[keep]
    else:
        derived = generalized.filter(pa.array(keep, type=pa.bool_()))
    if not _same_table(released.table, derived):
        raise ValueError("the released table differs from the verified release")
    return derived


def _model_entry(model: PrivacyModel) -> dict[str, Any]:
    c = getattr(model, "c", None)
    t = getattr(model, "t", None)
    return {
        "kind": model.kind,
        "sensitive": model.sensitive_column,
        "k": getattr(model, "k", None),
        "l": getattr(model, "l", None),
        "c": None if c is None else float(c),
        "t": None if t is None else float(t),
        # The outliers are exactly the rows of failing classes (checked above), so every
        # class left meets every model.
        "met": True,
    }


def _figures(rows: int | None, share: float | None, top: float | None, mean: float | None) -> Any:
    if rows is None:
        return None
    return {"highest": top, "average": mean, "records_at_risk": share, "rows_at_risk": rows}


def _risk_section(risk: ReidentificationRiskReport, supplied: bool) -> dict[str, Any]:
    return {
        "risk_threshold": risk.risk_threshold,
        "population": "supplied" if supplied else "not_supplied",
        "population_rows": risk.population_rows,
        "prosecutor": _figures(
            risk.rows_at_risk_prosecutor,
            risk.records_at_risk_prosecutor,
            risk.highest_prosecutor_risk,
            risk.average_prosecutor_risk,
        ),
        "journalist": _figures(
            risk.rows_at_risk_journalist,
            risk.records_at_risk_journalist,
            risk.highest_journalist_risk,
            risk.average_journalist_risk,
        ),
        "marketer": (
            None if risk.average_marketer_risk is None else {"average": risk.average_marketer_risk}
        ),
    }


def _assumptions(supplied: bool, models: tuple[PrivacyModel, ...]) -> list[str]:
    out = ["prosecutor_target_known_in_release"]
    if supplied:
        out.append("population_consistent_with_superset")
    out.append("suppressed_rows_not_released")
    if any(type(m) in (EqualTCloseness, OrderedTCloseness) for m in models):
        out.append("t_closeness_against_input_distribution")
    return out


def build_risk_utility_report(
    released: Released,
    lattice: Lattice,
    result: SearchResult,
    *,
    risk_threshold: float,
    population: pd.DataFrame | pa.Table | None = None,
) -> RiskUtilityReport:
    """The ``risk-utility-report/v1`` document for ``released``.

    ``released`` and ``result`` come from ``apply_search`` and ``search_lattice`` on one table
    and ``lattice``. ``risk_threshold`` (in ``(0, 1]``, required: no number is "very small" for
    every release) sets which rows count as at risk. ``population``, a table the release was
    drawn from (at level 0, same column types), enables the journalist and marketer figures;
    without it they are ``null``. Never modifies its inputs.
    """
    _check_arguments(released, lattice, result, population)
    check_risk_threshold(risk_threshold)
    derived = _verified_release(released, lattice, result)
    node = result.node or ()  # a found result always has a node (checked above)
    rows_suppressed = result.rows_suppressed or 0
    generalized_population = None if population is None else lattice.apply(population, node)
    risk = measure_reidentification_risk(
        derived,
        lattice.columns,
        risk_threshold=risk_threshold,
        population=generalized_population,
    )
    supplied = population is not None
    document = {
        "schema": RISK_UTILITY_REPORT_SCHEMA,
        "method": _METHOD,
        "quasi_identifiers": list(result.columns),
        "heights": list(result.heights),
        "node": list(node),
        "models": [_model_entry(m) for m in result.models],
        "search": {
            "lattice_size": result.lattice_size,
            "nodes_evaluated": result.nodes_evaluated,
            "metric": result.metric,
        },
        "utility": {
            "discernibility": result.discernibility,
            "loss_metric": result.loss_metric,
            "non_uniform_entropy": result.non_uniform_entropy,
        },
        "suppression": {
            "rows_input": result.rows,
            "suppression_limit": result.suppression_limit,
            "allowed_suppressed": result.allowed_suppressed,
            "rows_suppressed": rows_suppressed,
            "rows_released": result.rows - rows_suppressed,
        },
        "classes": {"class_count": result.class_count, "k_min": result.k_min},
        "risk": _risk_section(risk, supplied),
        "assumptions": _assumptions(supplied, result.models),
        "not_established": [
            t
            for t in _NOT_ESTABLISHED
            if not (supplied and t == "population_risk_without_population")
        ],
    }
    return RiskUtilityReport.model_validate(document)
