"""The models' ARX monotonicity flags are honest.

A flag of True means: the rows failing at a node include those failing at
any successor. Checked on generated data for the flagged models, and shown
false by pinned counterexamples for the others.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.anonymize.hierarchies import Lattice
from decoy_engine.quality.anonymity import (
    DistinctLDiversity,
    EntropyLDiversity,
    EqualTCloseness,
    FrequencySet,
    KAnonymity,
    OrderedTCloseness,
    PrivacyModel,
    RecursiveCLDiversity,
)

from ._fixtures import NON_MONOTONE_ENTROPY, NON_MONOTONE_EQUAL_T, two_level_lattice


def outliers(
    frame: pd.DataFrame, lattice: Lattice, model: PrivacyModel, node: tuple[int, ...]
) -> set[int]:
    applied = lattice.apply(frame, node)
    fs = FrequencySet.of(applied, lattice.columns, (model,))
    return set(np.flatnonzero(fs.row_mask(fs.failing((model,)))).tolist())


@st.composite
def tables(draw: Any) -> pd.DataFrame:
    n = draw(st.integers(0, 16))
    col = lambda values: draw(st.lists(st.sampled_from(values), min_size=n, max_size=n))
    return pd.DataFrame(
        {
            "age": col(list(range(20))),
            "zip": col(["120", "121", "130", "210"]),
            "dx": col(["a", "b", "c", None]),
        }
    )


@settings(derandomize=True, max_examples=40, deadline=None)
@given(
    frame=tables(),
    model=st.sampled_from([KAnonymity(2), KAnonymity(3), DistinctLDiversity("dx", 2)]),
)
def test_monotone_models_never_add_outliers(frame: pd.DataFrame, model: PrivacyModel) -> None:
    assert model.monotone_with_suppression
    lattice = two_level_lattice()
    for node in lattice.nodes():
        here = outliers(frame, lattice, model, node)
        for successor in lattice.successors(node):
            assert outliers(frame, lattice, model, successor) <= here


@pytest.mark.parametrize(
    ("model", "table", "node", "successor"),
    [
        (EntropyLDiversity("dx", 2), NON_MONOTONE_ENTROPY, (0, 2), (1, 2)),
        (EqualTCloseness("dx", 0.25), NON_MONOTONE_EQUAL_T, (0, 3), (1, 3)),
    ],
)
def test_non_monotone_flags_have_counterexamples(
    model: PrivacyModel,
    table: dict[str, list[Any]],
    node: tuple[int, ...],
    successor: tuple[int, ...],
) -> None:
    frame = pd.DataFrame(table)
    lattice = two_level_lattice()
    assert not outliers(frame, lattice, model, successor) <= outliers(frame, lattice, model, node)
    assert not model.monotone_with_suppression


def test_flags_match_arx() -> None:
    """ARX's constructor flags (monotonicWithSuppression) per criterion."""
    assert KAnonymity.monotone_with_suppression is True
    assert DistinctLDiversity.monotone_with_suppression is True
    assert EntropyLDiversity.monotone_with_suppression is False
    assert RecursiveCLDiversity.monotone_with_suppression is False
    assert EqualTCloseness.monotone_with_suppression is False
    assert OrderedTCloseness.monotone_with_suppression is False
