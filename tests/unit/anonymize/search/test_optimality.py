"""The search returns the brute-force optimum over every node of small lattices."""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.anonymize.hierarchies import Lattice, category_hierarchy
from decoy_engine.anonymize.search import (
    Discernibility,
    LossMetric,
    NonUniformEntropy,
    SearchResult,
    search_lattice,
)
from decoy_engine.quality.anonymity import (
    DistinctLDiversity,
    EntropyLDiversity,
    EqualTCloseness,
    KAnonymity,
    OrderedTCloseness,
    PrivacyModel,
    RecursiveCLDiversity,
)

from . import _reference as ref
from ._fixtures import NON_MONOTONE_ENTROPY, NON_MONOTONE_EQUAL_T, TIES, two_level_lattice

METRICS = {
    "discernibility": Discernibility(),
    "loss": LossMetric(),
    "non_uniform_entropy": NonUniformEntropy(),
}

MODEL_SETS: list[tuple[PrivacyModel, ...]] = [
    (KAnonymity(2),),
    (KAnonymity(3),),
    (KAnonymity(2), DistinctLDiversity("dx", 2)),
    (EntropyLDiversity("dx", 2),),
    (RecursiveCLDiversity("dx", 2, 2),),
    (EqualTCloseness("dx", 0.25),),
    (OrderedTCloseness("s", 0.3),),
    (KAnonymity(2), EqualTCloseness("s", 0.35)),
]


def lattice() -> Lattice:
    """age 4 levels x zip 4 levels x sex 3 levels = 48 nodes."""
    base = two_level_lattice()
    sex = category_hierarchy([("F", "P"), ("M", "P"), ("X", "Q")])
    return Lattice({**base.hierarchies, "sex": sex})


@st.composite
def tables(draw: Any) -> pd.DataFrame:
    n = draw(st.integers(0, 14))
    col = lambda values: draw(st.lists(st.sampled_from(values), min_size=n, max_size=n))
    return pd.DataFrame(
        {
            "age": col(list(range(20))),
            "zip": col(["120", "121", "130", "210"]),
            "sex": col(["F", "M", "X", None]),
            "dx": col(["a", "b", "c", None]),
            "s": pd.array(col([1, 2, 3, 5, None]), dtype="Int64"),
        }
    )


def assert_optimal(
    frame: pd.DataFrame,
    lat: Lattice,
    models: tuple[PrivacyModel, ...],
    limit: float,
    metric: str,
) -> SearchResult:
    result = search_lattice(frame, lat, models, suppression_limit=limit, metric=METRICS[metric])
    outcomes = ref.evaluate(frame, lat, models, limit)
    best = ref.best(outcomes, metric)
    if best is None:
        assert not result.found
        return result
    assert result.found and result.node is not None
    chosen = {o.node: o for o in outcomes}[result.node]
    assert chosen.feasible
    assert result.rows_suppressed == chosen.suppressed
    assert result.discernibility == chosen.discernibility
    assert result.loss_metric == pytest.approx(float(chosen.loss_metric), abs=1e-12)
    assert result.non_uniform_entropy == pytest.approx(
        chosen.non_uniform_entropy, rel=1e-12, abs=1e-12
    )
    if metric == "non_uniform_entropy":
        # Float sums from two summation orders: ties are compared to rounding.
        assert math.isclose(
            chosen.non_uniform_entropy, best.non_uniform_entropy, rel_tol=1e-9, abs_tol=1e-9
        )
        near = [
            o
            for o in outcomes
            if o.feasible
            and math.isclose(
                o.non_uniform_entropy, best.non_uniform_entropy, rel_tol=1e-9, abs_tol=1e-9
            )
        ]
        if len(near) == 1:
            # No float near-tie: the node itself is determined.
            assert result.node == best.node
    else:
        assert result.node == best.node
    return result


@settings(derandomize=True, max_examples=60, deadline=None)
@given(
    frame=tables(),
    models=st.sampled_from(MODEL_SETS),
    limit=st.sampled_from([0, 0.1, 0.3]),
    metric=st.sampled_from(sorted(METRICS)),
)
def test_optimal_against_brute_force(
    frame: pd.DataFrame, models: tuple[PrivacyModel, ...], limit: float, metric: str
) -> None:
    assert_optimal(frame, lattice(), models, limit, metric)


@pytest.mark.parametrize("metric", sorted(METRICS))
@pytest.mark.parametrize("limit", [0.1, 0.2, 0.3, 0.4])
def test_non_monotone_entropy_with_suppression(metric: str, limit: float) -> None:
    frame = pd.DataFrame(NON_MONOTONE_ENTROPY)
    models = (EntropyLDiversity("dx", 2),)
    outcomes = {o.node: o for o in ref.evaluate(frame, two_level_lattice(), models, limit)}
    assert not outcomes[(1, 2)].outliers <= outcomes[(0, 2)].outliers
    assert_optimal(frame, two_level_lattice(), models, limit, metric)


@pytest.mark.parametrize("metric", sorted(METRICS))
@pytest.mark.parametrize("limit", [0.1, 0.25, 0.4])
def test_non_monotone_equal_t_with_suppression(metric: str, limit: float) -> None:
    frame = pd.DataFrame(NON_MONOTONE_EQUAL_T)
    models = (EqualTCloseness("dx", 0.25),)
    outcomes = {o.node: o for o in ref.evaluate(frame, two_level_lattice(), models, limit)}
    assert not outcomes[(1, 3)].outliers <= outcomes[(0, 3)].outliers
    assert_optimal(frame, two_level_lattice(), models, limit, metric)


def test_generalizing_can_lower_the_loss_under_suppression() -> None:
    """The optimum (0, 3) sits above nodes whose loss already exceeds the best
    found when they are visited; pruning on the loss instead of its
    no-suppression bound would skip it and return (2, 1)."""
    frame = pd.DataFrame(NON_MONOTONE_ENTROPY)
    result = assert_optimal(
        frame, two_level_lattice(), (EntropyLDiversity("dx", 2),), 0.3, "discernibility"
    )
    assert result.node == (0, 3)


@pytest.mark.parametrize("case", sorted(TIES))
def test_tie_breaks(case: str) -> None:
    table, k, limit, metric, winner, runner_up = TIES[case]
    frame = pd.DataFrame(table)
    outcomes = {
        o.node: o for o in ref.evaluate(frame, two_level_lattice(), (KAnonymity(k),), limit)
    }
    assert ref.loss_of(outcomes[winner], metric) == ref.loss_of(outcomes[runner_up], metric)
    if case != "suppressed":
        assert outcomes[winner].suppressed == outcomes[runner_up].suppressed
    if case == "level_sum":
        assert sum(winner) < sum(runner_up) and runner_up < winner
    if case == "lexicographic":
        assert sum(winner) == sum(runner_up) and winner < runner_up
    result = assert_optimal(frame, two_level_lattice(), (KAnonymity(k),), limit, metric)
    assert result.node == winner
