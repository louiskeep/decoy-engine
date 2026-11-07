"""A7 (numeric cells): the model holds only snapshot fields plus a floored, rounded correlation."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula import MIN_FIT_ROWS, fit_gaussian_copula

from ._fixtures import numeric_frame, seed_bytes, snapshot

_TOP_KEYS = {"schema_version", "row_count", "columns", "correlation", "suppressed"}
_NUMERIC_KEYS = {"name", "kind", "dtype", "null_count", "non_null_count", "bin_edges", "bin_counts"}


def _correlated(n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    a = rng.standard_normal(n)
    return pd.DataFrame({"a": a, "b": a + rng.standard_normal(n)})


def test_model_columns_equal_snapshot_fields() -> None:
    frame = numeric_frame(2, n=800)
    snap = snapshot(frame)
    data = fit_gaussian_copula(frame, snap, seed=seed_bytes(1)).to_dict()
    assert set(data) == _TOP_KEYS
    for entry in data["columns"]:
        source = snap["columns"][entry["name"]]
        assert set(entry) == _NUMERIC_KEYS
        for key in ("kind", "dtype", "null_count", "non_null_count"):
            assert entry[key] == source[key]
        assert entry["bin_edges"] == source["stats"]["bin_edges"]
        assert entry["bin_counts"] == source["stats"]["bin_counts"]


def test_interior_values_are_absent() -> None:
    frame = numeric_frame(2, n=800)
    planted = 1.2345678901
    frame.loc[400, "y"] = planted
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    text = json.dumps(model.to_dict())
    assert "1.2345678901" not in text and "1.23456789" not in repr(model)
    assert repr(model).startswith("GaussianCopulaModel(rows=800")


def test_minimum_rows() -> None:
    small = _correlated(MIN_FIT_ROWS - 1)
    with pytest.raises(ValueError, match="at least 100 rows"):
        fit_gaussian_copula(small, snapshot(small), seed=seed_bytes(1))
    enough = _correlated(MIN_FIT_ROWS)
    assert fit_gaussian_copula(enough, snapshot(enough), seed=seed_bytes(1)).row_count == 100


def test_released_correlation_is_rounded_to_three_decimals() -> None:
    frame = numeric_frame(4, n=1000)
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    assert all(round(v, 3) == v for v in model.correlation)
    assert any(v != 0 for v in model.correlation)


@pytest.mark.parametrize(
    ("rows", "suppressed"), [(150, {(0, "rows_per_bin"), (1, "rows_per_bin")}), (200, set())]
)
def test_rows_per_bin(rows: int, suppressed: set[tuple[int, str]]) -> None:
    frame = _correlated(rows)
    model = fit_gaussian_copula(frame, snapshot(frame, numeric_bins=20), seed=seed_bytes(1))
    assert {(s.variable, s.reason) for s in model.suppressed} == suppressed
    if suppressed:
        assert model.correlation == (0.0,)


def test_constant_column_is_zero_variance() -> None:
    frame = _correlated(300)
    frame["k"] = 2.0
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    assert {(s.variable, s.reason) for s in model.suppressed} == {(2, "zero_variance")}
    m = model.correlation_matrix()
    assert np.all(m[2, :2] == 0) and m[0, 1] != 0


def test_latent_limit_and_pair_budget() -> None:
    rng = np.random.default_rng(0)
    wide = pd.DataFrame(rng.standard_normal((100, 257)), columns=[f"c{i}" for i in range(257)])
    with pytest.raises(ValueError, match="more than 256 latent"):
        fit_gaussian_copula(wide, snapshot(wide), seed=seed_bytes(1))
    budget = wide.iloc[:, :11]  # 55 pairs over 100 rows: above half the row count
    with pytest.raises(ValueError, match="half the row count"):
        fit_gaussian_copula(budget, snapshot(budget), seed=seed_bytes(1))
    fits = wide.iloc[:, :10]  # 45 pairs
    assert len(fit_gaussian_copula(fits, snapshot(fits), seed=seed_bytes(1)).correlation) == 45
