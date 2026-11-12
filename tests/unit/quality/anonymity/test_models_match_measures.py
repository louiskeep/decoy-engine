"""Each model fails exactly the classes its measure counts below (or above) target."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.quality.anonymity import (
    DistinctLDiversity,
    EntropyLDiversity,
    EqualTCloseness,
    FrequencySet,
    KAnonymity,
    OrderedTCloseness,
    PrivacyModel,
    RecursiveCLDiversity,
    measure_k_anonymity,
    measure_l_diversity,
    measure_t_closeness_equal,
    measure_t_closeness_ordered,
)

QI = ["a", "b"]


def failing_rows(frame: pd.DataFrame, model: PrivacyModel) -> int:
    fs = FrequencySet.of(frame, QI, (model,))
    return int(fs.row_mask(fs.failing((model,))).sum())


def measured_rows(frame: pd.DataFrame, model: PrivacyModel) -> int | None:
    if isinstance(model, KAnonymity):
        return measure_k_anonymity(frame, QI, k_target=model.k).rows_below_target
    if isinstance(model, DistinctLDiversity):
        return measure_l_diversity(frame, QI, "s", l_target=model.l).distinct_rows_below_target
    if isinstance(model, EntropyLDiversity):
        return measure_l_diversity(frame, QI, "s", l_target=model.l).entropy_rows_below_target
    if isinstance(model, RecursiveCLDiversity):
        report = measure_l_diversity(frame, QI, "s", l_target=model.l, recursive_c=model.c)
        return report.recursive_rows_below_target
    measure = (
        measure_t_closeness_equal
        if isinstance(model, EqualTCloseness)
        else measure_t_closeness_ordered
    )
    assert isinstance(model, (EqualTCloseness, OrderedTCloseness))
    t_report = measure(frame, QI, "s", t_target=model.t)
    assert t_report.rows_above_target is not None
    return t_report.rows_above_target + t_report.rows_in_unmeasured_classes


@st.composite
def frames(draw: Any) -> pd.DataFrame:
    n = draw(st.integers(0, 30))
    col = lambda values: draw(st.lists(st.sampled_from(values), min_size=n, max_size=n))
    return pd.DataFrame(
        {"a": col([0, 1, 2]), "b": col(["x", "y"]), "s": col([1.0, 2.0, 3.0, 4.0, np.nan])}
    )


MODELS = st.one_of(
    st.builds(KAnonymity, st.integers(1, 5)),
    st.builds(DistinctLDiversity, st.just("s"), st.integers(1, 4)),
    st.builds(EntropyLDiversity, st.just("s"), st.integers(1, 4)),
    st.builds(
        RecursiveCLDiversity, st.just("s"), st.sampled_from([0.5, 1, 2, 3.0]), st.integers(1, 4)
    ),
    st.builds(EqualTCloseness, st.just("s"), st.sampled_from([0.0, 0.1, 0.25, 0.5, 1.0])),
    st.builds(OrderedTCloseness, st.just("s"), st.sampled_from([0.0, 0.1, 0.25, 0.5, 1.0])),
)


@settings(derandomize=True, max_examples=200, deadline=None)
@given(frame=frames(), model=MODELS)
def test_models_match_measures(frame: pd.DataFrame, model: PrivacyModel) -> None:
    assert failing_rows(frame, model) == measured_rows(frame, model)


def test_uniform_class_meets_entropy_at_the_tolerance() -> None:
    """Three equally frequent values: H = ln 3 up to rounding; both sides pass it."""
    frame = pd.DataFrame({"a": [0] * 3, "b": ["x"] * 3, "s": [1.0, 2.0, 3.0]})
    model = EntropyLDiversity("s", 3)
    assert failing_rows(frame, model) == measured_rows(frame, model) == 0


@pytest.mark.parametrize(
    "build",
    [
        lambda: KAnonymity(True),
        lambda: KAnonymity(np.int64(2)),  # type: ignore[arg-type]
        lambda: KAnonymity(0),
        lambda: DistinctLDiversity("s", 2.0),  # type: ignore[arg-type]
        lambda: DistinctLDiversity(1, 2),  # type: ignore[arg-type]
        lambda: RecursiveCLDiversity("s", float("nan"), 2),
        lambda: RecursiveCLDiversity("s", 0, 2),
        lambda: EqualTCloseness("s", 1.5),
        lambda: EqualTCloseness("s", True),
        lambda: OrderedTCloseness("s", float("inf")),
    ],
)
def test_model_arguments_are_strict(build: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        build()


def test_models_are_value_free_descriptors() -> None:
    model = RecursiveCLDiversity("s", 2, 3)
    assert repr(model) == "RecursiveCLDiversity(sensitive='s', c=2, l=3)"
    assert math.isclose(model.c, 2)
