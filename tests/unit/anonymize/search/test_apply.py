"""apply_search: generalized table with suppressed rows removed, and its refusals."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.anonymize.hierarchies import Lattice, interval_hierarchy
from decoy_engine.anonymize.search import Released, apply_search, search_lattice
from decoy_engine.quality.anonymity import (
    EntropyLDiversity,
    EqualTCloseness,
    KAnonymity,
    measure_k_anonymity,
    measure_l_diversity,
)

from . import _reference as ref
from ._fixtures import NON_MONOTONE_EQUAL_T, seven_rows, six_rows, small_lattice, two_level_lattice


def _equal(left: pd.DataFrame | pa.Table, right: pd.DataFrame | pa.Table) -> None:
    if isinstance(left, pd.DataFrame):
        pd.testing.assert_frame_equal(left, right)
    else:
        assert left.equals(right)


@pytest.mark.parametrize("route", ["pandas", "arrow"])
def test_released_table_drops_the_suppressed_rows(route: str) -> None:
    frame = seven_rows()
    table = frame if route == "pandas" else pa.Table.from_pandas(frame, preserve_index=False)
    before = copy.deepcopy(table)
    lattice = small_lattice()
    models = (KAnonymity(2),)
    result = search_lattice(table, lattice, models, suppression_limit=0.15)
    released = apply_search(table, lattice, result)
    assert isinstance(released, Released)
    _equal(table, before)
    assert released.suppressed.dtype == np.bool_
    assert released.suppressed.tolist() == [False] * 6 + [True]
    assert int(released.suppressed.sum()) == result.rows_suppressed == 1
    full = lattice.apply(table, (1, 1))
    _equal(released.generalized_including_suppressed, full)
    if route == "pandas":
        _equal(released.table, full[~released.suppressed])
        assert len(released.table) == 6
    else:
        _equal(released.table, full.filter(pa.array(~released.suppressed)))
        assert released.table.num_rows == 6
    k = measure_k_anonymity(released.table, lattice.columns, k_target=2)
    assert k.meets_target is True


def test_released_classes_meet_l_and_t() -> None:
    frame = pd.DataFrame(NON_MONOTONE_EQUAL_T)
    lattice = two_level_lattice()
    models = (EntropyLDiversity("dx", 2), EqualTCloseness("dx", 0.3))
    result = search_lattice(frame, lattice, models, suppression_limit=0.3)
    assert result.found
    released = apply_search(frame, lattice, result)
    l_report = measure_l_diversity(released.table, lattice.columns, "dx", l_target=2)
    assert l_report.entropy_meets_target is True
    # t holds against the whole input's distribution, checked by the reference EMD.
    table_values = frame["dx"].tolist()
    groups: dict[tuple[object, ...], list[object]] = {}
    for _, row in released.table.iterrows():
        groups.setdefault(tuple(row[c] for c in lattice.columns), []).append(row["dx"])
    assert groups
    for values in groups.values():
        assert float(ref._equal_emd(values, table_values)) <= 0.3 + 1e-12


def test_refuses_a_result_that_found_nothing() -> None:
    lattice = small_lattice()
    result = search_lattice(six_rows(), lattice, (KAnonymity(7),))
    with pytest.raises(ValueError, match="found no node"):
        apply_search(six_rows(), lattice, result)


def test_refuses_another_lattice() -> None:
    result = search_lattice(six_rows(), small_lattice(), (KAnonymity(2),))
    other = Lattice(
        {"age": interval_hierarchy((10, 20)), "zip": small_lattice().hierarchies["zip"]}
    )
    with pytest.raises(ValueError, match="another lattice"):
        apply_search(six_rows(), other, result)


def test_refuses_another_table_with_the_same_shape() -> None:
    """Same columns and row count; on the second table (1, 1) suppresses nothing,
    so only the recomputed suppressed count can tell the tables apart."""
    lattice = small_lattice()
    result = search_lattice(seven_rows(), lattice, (KAnonymity(2),), suppression_limit=0.15)
    assert result.rows_suppressed == 1
    other = pd.concat([six_rows(), six_rows().iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="suppressed rows differ"):
        apply_search(other, lattice, result)


def test_refuses_another_row_count() -> None:
    lattice = small_lattice()
    result = search_lattice(six_rows(), lattice, (KAnonymity(2),))
    with pytest.raises(ValueError, match="row count"):
        apply_search(seven_rows(), lattice, result)
