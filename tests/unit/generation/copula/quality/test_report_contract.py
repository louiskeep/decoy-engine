"""A6: the report is value-free, frozen, JSON-safe, rounded, closed in its tokens, and leaves its
inputs alone."""

from __future__ import annotations

import copy
import dataclasses
import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from ._helpers import fit, report, sample

_TOP = {
    "pairwise": {
        "score",
        "correlation_score",
        "contingency_score",
        "scored",
        "withheld",
        "undefined",
    },
}
_KEYS = {"schema_version", "rows", "marginal", "pairs", "pairwise", "dcr", "synth_report"}
_STATUS = {
    "scored": {None},
    "withheld": {
        "suppressed_in_model",
        "pair_rows",
        "distinguished_rows",
        "high_cardinality",
        "release_budget",
    },
    "undefined": {"empty_column", "constant_in_synthetic", "too_few_synthetic_rows"},
}
_PERCENTILES = {"p05", "p25", "median", "p75", "p95"}


def _sentinel_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(30)
    top = [f"SENTINELTOP{i:02d}" for i in range(20)]
    tail = [f"SENTINELTAIL{i}" for i in range(5)]
    labels = np.array(top * 14 + tail + [top[0]] * 15, dtype=object)
    x = rng.normal(size=300)
    x[17] = 987654.321
    real = pd.DataFrame({"x": x, "k": rng.permutation(labels), "y": rng.normal(size=300)})
    holdout = real.iloc[:150].copy()
    holdout.loc[holdout.index[3], "k"] = "SENTINELHOLD"
    holdout.loc[holdout.index[4], "y"] = 13579.2468
    return real, holdout


def _floats(value: Any) -> list[float]:
    if isinstance(value, dict):
        return [f for v in value.values() for f in _floats(v)]
    if isinstance(value, list):
        return [f for v in value for f in _floats(v)]
    return [value] if isinstance(value, float) else []


def test_value_free() -> None:
    real, holdout = _sentinel_frames()
    result = report(real, fit(real), holdout=holdout)
    text = json.dumps(result.to_dict(), allow_nan=False) + repr(result)
    for planted in ("SENTINEL", "987654", "13579"):
        assert planted not in text


def test_frozen_and_fresh_dicts() -> None:
    real, _ = _sentinel_frames()
    result = report(real, fit(real))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.pairwise_score = 1.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.pairs[0].score = 1.0  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.synth_report["severity"] = "ok"  # type: ignore[index]
    first = result.to_dict()
    first["pairs"].clear()
    first["synth_report"]["disclaimers"].clear()
    assert result.to_dict()["pairs"] and result.to_dict()["synth_report"]["disclaimers"]


def test_shape_rounding_and_closed_sets() -> None:
    real, holdout = _sentinel_frames()
    data = report(real, fit(real), holdout=holdout).to_dict()
    assert set(data) == _KEYS
    assert data["schema_version"] == "decoy-copula-quality-report/v1"
    assert set(data["pairwise"]) == _TOP["pairwise"]
    for block in ("marginal", "pairs", "pairwise", "dcr"):
        for value in _floats(data[block]):
            assert value == round(value, 3)
    for p in data["pairs"]:
        assert p["reason"] in _STATUS[p["status"]]
        assert p["metric"] in {"spearman_correlation_similarity", "contingency_similarity"}
    dcr = data["dcr"]
    for summary in (
        dcr["synthetic_to_real"],
        dcr["real_to_real"],
        dcr["holdout"]["synthetic_to_holdout"],
    ):
        assert set(summary["withheld"]) <= _PERCENTILES
        assert set(summary["withheld"].values()) <= {"percentile_rows"}


def test_marginal_block_holds_no_real_quantile() -> None:
    rng = np.random.default_rng(31)
    x = rng.uniform(0, 10, 300)
    real = pd.DataFrame({"x": x, "y": rng.normal(size=300)})
    model = fit(real)
    synth = sample(model)
    edges = np.asarray(model.columns[0].bin_edges)
    shifted = real.copy()
    inner = (x > x.min()) & (x < x.max())
    bins = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, len(edges) - 2)
    centre = (edges[bins] + edges[bins + 1]) / 2
    shifted.loc[inner, "x"] = x[inner] + 0.5 * (centre[inner] - x[inner])
    a = report(real, model, synth).to_dict()["marginal"]
    b = report(shifted, model, synth).to_dict()["marginal"]
    assert a == b
    assert all(set(entry) == {"column", "gof_method", "gof_value"} for entry in a["columns"])


def test_inputs_unchanged() -> None:
    # Columns whose canonical form differs from the input: nullable ints, object NaN, bool.
    rng = np.random.default_rng(32)
    real = pd.DataFrame(
        {
            "n": pd.array(rng.integers(0, 50, 300), dtype="Int64"),
            "c": rng.choice(np.array(["a", "b", "c"], dtype=object), 300),
            "f": rng.random(300) < 0.4,
        }
    )
    real.loc[:19, "n"] = pd.NA
    real.loc[20:39, "c"] = np.nan
    model = fit(real)
    synth = sample(model)
    holdout = real.iloc[:150].copy()
    before = [copy.deepcopy(f) for f in (real, synth, holdout)]
    model_before = model.to_dict()
    report(real, model, synth, holdout=holdout)
    for frame, saved in zip((real, synth, holdout), before, strict=True):
        pd.testing.assert_frame_equal(frame, saved, check_exact=True)
    assert model.to_dict() == model_before
