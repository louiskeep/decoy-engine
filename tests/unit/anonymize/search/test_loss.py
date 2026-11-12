"""Loss metrics: the no-suppression bound is below the value and grows up the lattice."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.anonymize.search._loss import Losses, losses
from decoy_engine.anonymize.search._search import _prepare
from decoy_engine.quality.anonymity import EntropyLDiversity, KAnonymity, PrivacyModel

from . import _reference as ref
from ._fixtures import two_level_lattice


def _all(
    frame: pd.DataFrame, models: tuple[PrivacyModel, ...]
) -> dict[tuple[int, ...], tuple[Losses, Losses, int]]:
    lattice = two_level_lattice()
    prepared = _prepare(frame, lattice, models)
    out = {}
    for node in lattice.nodes():
        node_set = prepared.base.rollup(
            [c.levels[h].codes for c, h in zip(prepared.columns, node, strict=True)]
        )
        failing = node_set.failing(models)
        value, bound = losses(
            prepared.columns,
            node,
            prepared.base.sizes,
            node_set.sizes,
            ~failing,
            failing[node_set.base_class],
        )
        out[node] = (value, bound, int(node_set.sizes[failing].sum()))
    return out


@st.composite
def tables(draw: Any) -> pd.DataFrame:
    n = draw(st.integers(0, 14))
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
    models=st.sampled_from([(KAnonymity(2),), (KAnonymity(3),), (EntropyLDiversity("dx", 2),)]),
)
def test_bound_is_below_and_monotone(frame: pd.DataFrame, models: tuple[PrivacyModel, ...]) -> None:
    lattice = two_level_lattice()
    table = _all(frame, models)
    reference = {o.node: o for o in ref.evaluate(frame, lattice, models, 1.0)}
    for node, (value, bound, suppressed) in table.items():
        assert value.discernibility == reference[node].discernibility
        assert value.loss_metric == reference[node].loss_metric
        assert abs(value.non_uniform_entropy - reference[node].non_uniform_entropy) <= 1e-9
        assert bound.discernibility <= value.discernibility
        assert bound.loss_metric <= value.loss_metric
        assert bound.non_uniform_entropy <= value.non_uniform_entropy + 1e-9
        if suppressed == 0:
            assert bound == value
        for successor in lattice.successors(node):
            higher = table[successor][1]
            assert bound.discernibility <= higher.discernibility
            assert bound.loss_metric <= higher.loss_metric
            assert bound.non_uniform_entropy <= higher.non_uniform_entropy + 1e-9


def test_constant_column_loses_nothing_in_the_loss_metric() -> None:
    frame = pd.DataFrame({"age": [3, 3, 3, 3], "zip": ["120", "120", "121", "121"]})
    table = _all(frame, (KAnonymity(2),))
    value, _, suppressed = table[(3, 1)]
    assert suppressed == 0
    # age has one observed value (D = 1): 0. zip "12*" covers both values: share 1 each row.
    # (0 + 4 * 1) / (4 rows * 2 columns) = 1/2
    assert value.loss_metric == Fraction(1, 2)
