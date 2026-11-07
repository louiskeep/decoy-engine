"""A7 floor cells: near-constant numeric columns, boundaries and tie-breaks of every floor, and
the eigenvalue floor that lets the 3-decimal release skip the shrink ladder."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula import GaussianCopulaModel, _fit, _floors, fit_gaussian_copula
from decoy_engine.generation.copula._nearest_corr import min_eigenvalue, nearest_correlation

from ._fixtures import seed_bytes, snapshot


def _suppressed(model: GaussianCopulaModel) -> set[tuple[int, str]]:
    return {(s.variable, s.reason) for s in model.suppressed}


def _people(rows: int = 1000, flagged: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(417)
    flag = np.zeros(rows, dtype=np.int64)
    flag[rng.choice(rows, flagged, replace=False)] = 1
    return pd.DataFrame(
        {
            "age": rng.integers(18, 90, rows),
            "income": rng.lognormal(10, 0.5, rows),
            "zip3": rng.integers(100, 999, rows),
            "flag": flag,
        }
    )


@pytest.mark.parametrize(("flagged", "suppressed"), [(1, True), (10, True), (11, False)])
def test_near_constant_numeric_column_is_suppressed(flagged: int, suppressed: bool) -> None:
    frame = _people(flagged=flagged)
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    assert _suppressed(model) == ({(3, "distinguished_rows")} if suppressed else set())
    if suppressed:
        assert np.all(np.delete(model.correlation_matrix()[3], 3) == 0)


@pytest.mark.parametrize(("rows", "suppressed"), [(199, True), (200, False)])
def test_rows_per_bin_boundary(rows: int, suppressed: bool) -> None:
    rng = np.random.default_rng(9)
    a = rng.standard_normal(rows)
    frame = pd.DataFrame({"a": a, "b": a + rng.standard_normal(rows)})
    model = fit_gaussian_copula(frame, snapshot(frame, numeric_bins=20), seed=seed_bytes(1))
    expected = {(0, "rows_per_bin"), (1, "rows_per_bin")} if suppressed else set()
    assert _suppressed(model) == expected


def test_pair_floor_tie_suppresses_the_later_variable() -> None:
    rng = np.random.default_rng(10)
    index = np.arange(300)
    base = rng.standard_normal(300)
    frame = pd.DataFrame(
        {
            "a": np.where(index < 100, base, np.nan),
            "b": np.where((index >= 60) & (index < 160), base + rng.standard_normal(300), np.nan),
        }
    )
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    # Latent order: a, a-null, b, b-null; a and b share 40 rows and both have 100.
    assert _suppressed(model) == {(2, "pair_rows")}


def test_constant_on_the_shared_rows_is_zero_variance() -> None:
    rng = np.random.default_rng(11)
    index = np.arange(300)
    frame = pd.DataFrame(
        {
            "a": np.where(index < 150, rng.standard_normal(300), 5.0),
            "b": np.where(index >= 150, rng.standard_normal(300), np.nan),
        }
    )
    model = fit_gaussian_copula(frame, snapshot(frame, numeric_bins=4), seed=seed_bytes(1))
    assert _suppressed(model) == {(0, "zero_variance")}


def _near_singular(rng: np.random.Generator, d: int) -> np.ndarray:
    base = rng.standard_normal((400, d - 1))
    data = np.column_stack([base, base[:, 0] * 2 + 1e-3 * rng.standard_normal(400)])
    return np.corrcoef(data, rowvar=False)


@pytest.mark.parametrize("d", [4, 8, 12, 20])
def test_release_floor_lets_rounding_skip_the_shrink_ladder(d: int) -> None:
    raw = _near_singular(np.random.default_rng(d), d)
    none = np.zeros((d, d), dtype=bool)
    out = _fit.release(raw, list(range(d)), none)
    repaired = nearest_correlation(raw, floor=_floors.rounding_floor(d))
    assert np.array_equal(out, _fit._round_release(repaired))
    assert min_eigenvalue(out) >= _floors.MIN_EIGENVALUE


def test_suppressed_entries_must_be_unique_and_ascending() -> None:
    rng = np.random.default_rng(12)
    frame = pd.DataFrame(rng.standard_normal((300, 3)), columns=["a", "b", "c"])
    data = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1)).to_dict()
    data["correlation"] = [0.0, 0.0, 0.0]
    good = copy.deepcopy(data)
    good["suppressed"] = [
        {"variable": 0, "reason": "pair_rows"},
        {"variable": 1, "reason": "pair_rows"},
    ]
    GaussianCopulaModel.from_dict(good)
    for bad in (
        [{"variable": 1, "reason": "pair_rows"}, {"variable": 0, "reason": "pair_rows"}],
        [{"variable": 1, "reason": "pair_rows"}, {"variable": 1, "reason": "zero_variance"}],
    ):
        data["suppressed"] = bad
        with pytest.raises(ValueError, match="unique and in ascending order"):
            GaussianCopulaModel.from_dict(copy.deepcopy(data))


def test_value_and_its_null_indicator_are_exactly_zero() -> None:
    rng = np.random.default_rng(13)
    a = rng.standard_normal(400)
    b = a + rng.standard_normal(400)
    b[rng.random(400) < 0.3] = np.nan
    frame = pd.DataFrame({"a": a, "b": b})
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    matrix = model.correlation_matrix()
    assert matrix[1, 2] == 0.0 and matrix[0, 1] != 0 and matrix[0, 2] != 0
    data = model.to_dict()
    data["correlation"][2] = 0.05  # lower triangle order: (1,0), (2,0), (2,1)
    with pytest.raises(ValueError, match="its own null indicator"):
        GaussianCopulaModel.from_dict(data)
