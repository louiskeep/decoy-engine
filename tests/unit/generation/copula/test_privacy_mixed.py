"""A7 (categorical and null cells), the variable-level suppression contract, and the encoding
rules a later DP snapshot input relies on."""

from __future__ import annotations

import copy
import json

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula import (
    GaussianCopulaModel,
    _fit,
    _floors,
    fit_gaussian_copula,
)
from decoy_engine.generation.copula._marginals import (
    STATE_LABEL,
    decode_states,
    from_snapshot_entry,
    intervals_for,
)
from decoy_engine.generation.copula._model import (
    KIND_CATEGORICAL,
    KIND_NUMERIC,
    ROLE_STATE,
    ColumnMarginal,
    latent_layout,
    structural_zeros,
)
from decoy_engine.generation.copula._nearest_corr import nearest_correlation

from ._fixtures import fit_stream, mixed_frame, seed_bytes, snapshot

TAIL = "zz-tail-label"


def _suppressed(model: GaussianCopulaModel) -> set[tuple[int, str]]:
    return {(s.variable, s.reason) for s in model.suppressed}


def test_categorical_entries_equal_snapshot_and_tail_is_absent() -> None:
    rng = np.random.default_rng(1)
    labels = np.array([f"L{k:02d}" for k in range(24)] + [TAIL], dtype=object)
    weights = np.r_[np.linspace(3, 2, 24), 0.2]
    frame = pd.DataFrame(
        {"v": rng.standard_normal(3000), "k": rng.choice(labels, 3000, p=weights / weights.sum())}
    )
    snap = snapshot(frame)
    model = fit_gaussian_copula(frame, snap, seed=seed_bytes(1))
    entry = model.to_dict()["columns"][1]
    source = snap["columns"]["k"]
    assert entry["top_values"] == source["stats"]["top_values"]
    assert entry["other_count"] == source["stats"]["other_count"] > 0
    assert set(entry) == {
        "name",
        "kind",
        "dtype",
        "null_count",
        "non_null_count",
        "top_values",
        "other_count",
    }
    assert TAIL not in json.dumps(model.to_dict()) and TAIL not in repr(model)
    assert not any(e["value"] in repr(model) for e in entry["top_values"])


@pytest.mark.parametrize(("shared", "expected"), [(49, {(1, "pair_rows")}), (50, set())])
def test_pair_rows_suppresses_the_sparser_variable(
    shared: int, expected: set[tuple[int, str]]
) -> None:
    rng = np.random.default_rng(2)
    a = rng.standard_normal(200)
    b = np.where(np.arange(200) < shared, a + rng.standard_normal(200), np.nan)
    frame = pd.DataFrame({"a": a, "b": b, "c": a + rng.standard_normal(200)})
    model = fit_gaussian_copula(frame, snapshot(frame, numeric_bins=4), seed=seed_bytes(1))
    assert _suppressed(model) == expected
    matrix = model.correlation_matrix()
    if expected:
        assert np.all(np.delete(matrix[1], 1) == 0)


def test_variable_suppression_leaves_the_rest_as_if_absent() -> None:
    rng = np.random.default_rng(3)
    a = rng.standard_normal(300)
    frame = pd.DataFrame(
        {
            "a": a,
            "b": np.where(np.arange(300) < 40, a + rng.standard_normal(300), np.nan),
            "c": a + 0.5 * rng.standard_normal(300),
            "d": -a + rng.standard_normal(300),
        }
    )
    snap = snapshot(frame, numeric_bins=4)
    model = fit_gaussian_copula(frame, snap, seed=seed_bytes(1))
    assert _suppressed(model) == {(1, "pair_rows")}
    columns = tuple(from_snapshot_entry(c, snap["columns"][c]) for c in frame.columns)
    layout = latent_layout(columns)
    scores = _fit.latent_scores(frame, columns, layout, fit_stream(seed_bytes(1)))
    stats = _fit._PairStats(scores)
    kept = [i for i in range(len(layout)) if i != 1]
    structural = structural_zeros(layout)[np.ix_(kept, kept)]
    reduced = stats.correlation[np.ix_(kept, kept)].copy()
    reduced[structural] = 0.0
    np.fill_diagonal(reduced, 1.0)
    repaired = nearest_correlation(
        reduced, floor=_floors.rounding_floor(len(kept)), fixed_zero=structural
    )
    released = model.correlation_matrix()
    assert np.allclose(released[np.ix_(kept, kept)], np.round(repaired, 3), atol=1e-12, rtol=0)
    assert np.all(np.delete(released[1], 1) == 0)


@pytest.mark.parametrize(("nulls", "suppressed"), [(1, True), (10, True), (11, False)])
def test_rare_nulls_suppress_the_indicator(nulls: int, suppressed: bool) -> None:
    rng = np.random.default_rng(4)
    a = rng.standard_normal(300)
    b = a + rng.standard_normal(300)
    b[:nulls] = np.nan
    frame = pd.DataFrame({"a": a, "b": b})
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    assert _suppressed(model) == ({(2, "distinguished_rows")} if suppressed else set())


@pytest.mark.parametrize(("rare", "suppressed"), [(10, True), (11, False)])
def test_near_constant_categorical_is_suppressed(rare: int, suppressed: bool) -> None:
    rng = np.random.default_rng(5)
    frame = pd.DataFrame(
        {
            "a": rng.standard_normal(300),
            "k": np.array(["m"] * (300 - rare) + ["r"] * rare, dtype=object),
        }
    )
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    assert _suppressed(model) == ({(1, "distinguished_rows")} if suppressed else set())


def _categorical_dict() -> dict:
    frame = mixed_frame(0, n=400)[["x", "c"]]
    return fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1)).to_dict()


@pytest.mark.parametrize("corrupt", ["out of order", "duplicate", "bool count", "zero count"])
def test_categorical_dict_cells_are_refused(corrupt: str) -> None:
    data = copy.deepcopy(_categorical_dict())
    top = data["columns"][1]["top_values"]
    for entry in top:
        entry["value"] = "zz-planted-" + entry["value"]
    GaussianCopulaModel.from_dict(copy.deepcopy(data))  # relabelled but still valid
    if corrupt == "out of order":
        top[0], top[1] = top[1], top[0]
    elif corrupt == "duplicate":
        top[1]["value"] = top[0]["value"]
    elif corrupt == "bool count":
        top[0]["count"] = True
    else:
        top[-1]["count"] = 0
    with pytest.raises((ValueError, TypeError)) as info:
        GaussianCopulaModel.from_dict(data)
    assert "zz-planted" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_label_outside_the_marginal_without_a_tail_is_refused() -> None:
    frame = pd.DataFrame({"k": np.array(["a", "b", "zz-unseen"] * 40, dtype=object)})
    column = ColumnMarginal(
        "k", KIND_CATEGORICAL, "object", 0, 120, top_values=(("a", 40), ("b", 40), ("c", 40))
    )
    with pytest.raises(ValueError, match="outside its marginal") as info:
        _fit.latent_scores(frame, (column,), latent_layout((column,)), fit_stream(seed_bytes(1)))
    assert "zz-unseen" not in str(info.value)


def test_kind_disagreeing_with_dtype_is_refused() -> None:
    frame = pd.DataFrame({"k": np.array(["a", "b"] * 60, dtype=object)})
    column = ColumnMarginal(
        "k", KIND_NUMERIC, "float64", 0, 120, bin_edges=(0.0, 1.0), bin_counts=(120,)
    )
    with pytest.raises(ValueError, match="kind does not match"):
        _fit.latent_scores(frame, (column,), latent_layout((column,)), fit_stream(seed_bytes(1)))


def test_state_lookup_accepts_the_closed_interval() -> None:
    column = ColumnMarginal(
        "k",
        KIND_CATEGORICAL,
        "object",
        1,
        1000,
        top_values=(("big", 999), ("tiny", 1)),
        other_count=0,
    )
    intervals = intervals_for(column, ROLE_STATE)
    index = decode_states(intervals, np.array([0.0, 1.0, np.nextafter(1.0, 0.0), 0.5]))
    assert np.all((index >= 0) & (index < intervals.kept))
    assert intervals.states[index[0]].kind == STATE_LABEL
