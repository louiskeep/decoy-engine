"""The report copies the search's figures, checks the release, and refuses foreign inputs."""

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.anonymize.report import RISK_UTILITY_REPORT_SCHEMA, build_risk_utility_report
from decoy_engine.anonymize.search import (
    Discernibility,
    LossMetric,
    NonUniformEntropy,
    Released,
    apply_search,
    search_lattice,
)
from decoy_engine.quality.anonymity import (
    DistinctLDiversity,
    EntropyLDiversity,
    EqualTCloseness,
    KAnonymity,
    OrderedTCloseness,
    RecursiveCLDiversity,
    measure_k_anonymity,
)

from ._helpers import release, table

# report path -> SearchResult attribute
COPIED = {
    ("quasi_identifiers",): "columns",
    ("heights",): "heights",
    ("node",): "node",
    ("search", "lattice_size"): "lattice_size",
    ("search", "nodes_evaluated"): "nodes_evaluated",
    ("search", "metric"): "metric",
    ("utility", "discernibility"): "discernibility",
    ("utility", "loss_metric"): "loss_metric",
    ("utility", "non_uniform_entropy"): "non_uniform_entropy",
    ("suppression", "rows_input"): "rows",
    ("suppression", "suppression_limit"): "suppression_limit",
    ("suppression", "allowed_suppressed"): "allowed_suppressed",
    ("suppression", "rows_suppressed"): "rows_suppressed",
    ("classes", "class_count"): "class_count",
    ("classes", "k_min"): "k_min",
}


def _at(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    for part in path:
        payload = payload[part]
    return payload


@pytest.mark.parametrize("metric", [Discernibility(), LossMetric(), NonUniformEntropy()])
@pytest.mark.parametrize("limit", [0.0, 0.34])
def test_copied_fields_equal_the_search_result(metric: Any, limit: float) -> None:
    the_lattice, result, released = release(table(), (KAnonymity(3),), limit, metric=metric)
    assert result.found
    payload = build_risk_utility_report(
        released, the_lattice, result, risk_threshold=0.2
    ).to_payload()
    for path, attribute in COPIED.items():
        want = getattr(result, attribute)
        got = _at(payload, path)
        assert (tuple(got) if isinstance(got, list) else got) == want, path
    assert payload["schema"] == RISK_UTILITY_REPORT_SCHEMA == "risk-utility-report/v1"
    assert payload["method"] == "full_domain_generalization_with_record_suppression"
    suppression = payload["suppression"]
    assert suppression["rows_released"] == len(released.table)
    assert suppression["rows_released"] == result.rows - (result.rows_suppressed or 0)
    k = measure_k_anonymity(released.table, ["age", "zip"])
    assert payload["classes"] == {"class_count": k.class_count, "k_min": k.k_min}


ALL_MODELS = (
    KAnonymity(2),
    DistinctLDiversity("dx", 1),
    EntropyLDiversity("dx", 1),
    RecursiveCLDiversity("dx", 2, 1),
    EqualTCloseness("dx", 1),
    OrderedTCloseness("visits", 1.0),
)


def test_models_are_described_and_met() -> None:
    the_lattice, result, released = release(table(), ALL_MODELS)
    payload = build_risk_utility_report(
        released, the_lattice, result, risk_threshold=0.2
    ).to_payload()
    assert payload["models"] == [
        {
            "kind": "k_anonymity",
            "sensitive": None,
            "k": 2,
            "l": None,
            "c": None,
            "t": None,
            "met": True,
        },
        {
            "kind": "distinct_l_diversity",
            "sensitive": "dx",
            "k": None,
            "l": 1,
            "c": None,
            "t": None,
            "met": True,
        },
        {
            "kind": "entropy_l_diversity",
            "sensitive": "dx",
            "k": None,
            "l": 1,
            "c": None,
            "t": None,
            "met": True,
        },
        {
            "kind": "recursive_cl_diversity",
            "sensitive": "dx",
            "k": None,
            "l": 1,
            "c": 2.0,
            "t": None,
            "met": True,
        },
        {
            "kind": "equal_t_closeness",
            "sensitive": "dx",
            "k": None,
            "l": None,
            "c": None,
            "t": 1.0,
            "met": True,
        },
        {
            "kind": "ordered_t_closeness",
            "sensitive": "visits",
            "k": None,
            "l": None,
            "c": None,
            "t": 1.0,
            "met": True,
        },
    ]


def test_t_assumption_only_with_a_t_model() -> None:
    the_lattice, result, released = release(table(), ALL_MODELS)
    with_t = build_risk_utility_report(released, the_lattice, result, risk_threshold=0.2)
    assert "t_closeness_against_input_distribution" in with_t.to_payload()["assumptions"]
    the_lattice, result, released = release(table(), ALL_MODELS[:4])
    without = build_risk_utility_report(released, the_lattice, result, risk_threshold=0.2)
    assert "t_closeness_against_input_distribution" not in without.to_payload()["assumptions"]


def _with_table(released: Released, new_table: Any) -> Released:
    return Released(
        table=new_table,
        suppressed=released.suppressed,
        generalized_including_suppressed=released.generalized_including_suppressed,
    )


def _suppressing() -> tuple[Any, Any, Released]:
    the_lattice, result, released = release(table(), (KAnonymity(3),), 0.34)
    assert result.found and 0 < (result.rows_suppressed or 0) < result.rows
    return the_lattice, result, released


def test_a_changed_cell_in_the_released_table_is_refused() -> None:
    the_lattice, result, released = _suppressing()
    edited = released.table.copy()
    edited.iloc[0, edited.columns.get_loc("age")] = "edited"
    with pytest.raises(ValueError, match="released table"):
        build_risk_utility_report(
            _with_table(released, edited), the_lattice, result, risk_threshold=0.2
        )


def test_a_changed_cell_in_an_arrow_release_is_refused() -> None:
    frame = pa.Table.from_pandas(table(), preserve_index=False)
    the_lattice, result, released = release(frame, (KAnonymity(3),), 0.34)
    column = released.table.column("dx").to_pylist()
    column[0] = "zz"
    edited = released.table.set_column(2, "dx", pa.array(column))
    with pytest.raises(ValueError, match="released table"):
        build_risk_utility_report(
            _with_table(released, edited), the_lattice, result, risk_threshold=0.2
        )


@pytest.mark.parametrize(("before", "after"), [(1, 1.0), (1, True), (0, False)])
def test_an_edit_that_equals_misses_is_refused(before: Any, after: Any) -> None:
    """``DataFrame.equals`` counts 1, 1.0 and True as equal in an object column; the class
    identity does not, so the check compares type families too."""
    frame = table().assign(code=[before] * 12)
    frame["code"] = frame["code"].astype(object)
    the_lattice, result, released = release(frame, (KAnonymity(3),), 0.34)
    edited = released.table.copy()
    edited.iloc[0, edited.columns.get_loc("code")] = after
    assert edited.equals(released.table)
    with pytest.raises(ValueError, match="released table"):
        build_risk_utility_report(
            _with_table(released, edited), the_lattice, result, risk_threshold=0.2
        )


def test_a_replaced_row_is_refused() -> None:
    the_lattice, result, released = _suppressing()
    replaced = pd.concat([released.table.iloc[1:], released.table.iloc[:1]])
    with pytest.raises(ValueError, match="released table"):
        build_risk_utility_report(
            _with_table(released, replaced), the_lattice, result, risk_threshold=0.2
        )


def test_an_altered_mask_with_the_same_count_is_refused() -> None:
    the_lattice, result, released = _suppressing()
    mask = released.suppressed.copy()
    first_true = int(np.flatnonzero(mask)[0])
    first_false = int(np.flatnonzero(~mask)[0])
    mask[first_true], mask[first_false] = False, True
    forged = Released(
        table=released.generalized_including_suppressed[~mask],
        suppressed=mask,
        generalized_including_suppressed=released.generalized_including_suppressed,
    )
    with pytest.raises(ValueError, match="suppressed rows"):
        build_risk_utility_report(forged, the_lattice, result, risk_threshold=0.2)


def test_a_release_of_another_table_is_refused() -> None:
    the_lattice, result, _ = _suppressing()
    other = table()
    other["zip"] = ["999"] * len(other)
    other_lattice, other_result, other_released = release(other, (KAnonymity(1),))
    with pytest.raises(ValueError):
        build_risk_utility_report(other_released, the_lattice, result, risk_threshold=0.2)


def test_refusals() -> None:
    the_lattice, result, released = release(table(), (KAnonymity(2),))
    with pytest.raises(TypeError):
        build_risk_utility_report(released.table, the_lattice, result, risk_threshold=0.2)
    with pytest.raises(TypeError):
        build_risk_utility_report(released, "lattice", result, risk_threshold=0.2)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_risk_utility_report(released, the_lattice, {}, risk_threshold=0.2)  # type: ignore[arg-type]
    taus: tuple[Any, ...] = (True, 0, 1.5, float("nan"), "0.2")
    for tau in taus:
        with pytest.raises((TypeError, ValueError)):
            build_risk_utility_report(released, the_lattice, result, risk_threshold=tau)
    with pytest.raises(TypeError):
        build_risk_utility_report(released, the_lattice, result, risk_threshold=0.2, population=[1])
    missing = search_lattice(table(), the_lattice, (KAnonymity(13),))
    assert not missing.found
    with pytest.raises(ValueError, match="no node"):
        build_risk_utility_report(released, the_lattice, missing, risk_threshold=0.2)


def test_a_result_of_another_lattice_is_refused() -> None:
    from decoy_engine.anonymize.hierarchies import Lattice, interval_hierarchy, prefix_hierarchy

    the_lattice, result, released = release(table(), (KAnonymity(2),))
    other = Lattice({"age": interval_hierarchy((5,)), "zip": prefix_hierarchy(3, keep=(2, 1))})
    with pytest.raises(ValueError, match="another lattice"):
        build_risk_utility_report(released, other, result, risk_threshold=0.2)


@pytest.mark.parametrize("arrow", [False, True])
def test_inputs_are_not_modified(arrow: bool) -> None:
    frame = table()
    source = pa.Table.from_pandas(frame, preserve_index=False) if arrow else frame
    the_lattice, result, released = release(source, (KAnonymity(3),), 0.34)
    population = pd.concat([frame, frame], ignore_index=True)
    before = (copy.deepcopy(released), population.copy())
    build_risk_utility_report(
        released, the_lattice, result, risk_threshold=0.2, population=population
    )
    if arrow:
        assert released.table.equals(before[0].table)
    else:
        pd.testing.assert_frame_equal(released.table, before[0].table)
    assert np.array_equal(released.suppressed, before[0].suppressed)
    pd.testing.assert_frame_equal(population, before[1])


def test_apply_search_output_is_accepted_for_both_routes() -> None:
    frame = table()
    for source in (frame, pa.Table.from_pandas(frame, preserve_index=False)):
        the_lattice, result, _ = release(source, (KAnonymity(3),), 0.34)
        released = apply_search(source, the_lattice, result)
        build_risk_utility_report(released, the_lattice, result, risk_threshold=0.2)
