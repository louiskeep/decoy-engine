"""A7 floor cells on shared rows: a pairwise-complete correlation sees only the rows both sides
observe, so the distinguished-rows floor is counted on those rows too, for value, categorical and
null-indicator partners. Also the structural zero under an indefinite raw matrix, and the exact
eigenvalue floor of the repair."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula import GaussianCopulaModel, _fit, fit_gaussian_copula
from decoy_engine.generation.copula._marginals import from_snapshot_entry
from decoy_engine.generation.copula._model import latent_layout
from decoy_engine.generation.copula._nearest_corr import min_eigenvalue, nearest_correlation

from ._fixtures import fit_stream, seed_bytes, snapshot


def _suppressed(model: GaussianCopulaModel) -> set[tuple[int, str]]:
    return {(s.variable, s.reason) for s in model.suppressed}


def _sparse_partner(
    rng: np.random.Generator, rows: int, shared_flagged: int, observed: int = 60
) -> tuple[np.ndarray, np.ndarray]:
    """A 0/1 flag on 30 rows and a value observed on `observed` rows, `shared_flagged` of them
    flagged."""
    y_rows = np.arange(observed)
    flag = np.zeros(rows, dtype=np.int64)
    flag[y_rows[:shared_flagged]] = 1
    flag[observed : observed + 30 - shared_flagged] = 1
    y = np.full(rows, np.nan)
    y[y_rows] = rng.standard_normal(observed)
    return flag, y


@pytest.mark.parametrize(("shared", "suppressed"), [(1, True), (10, True), (11, False)])
def test_numeric_flag_on_shared_rows(shared: int, suppressed: bool) -> None:
    rng = np.random.default_rng(29)
    flag, y = _sparse_partner(rng, 1000, shared)
    frame = pd.DataFrame({"a": rng.standard_normal(1000), "flag": flag, "y": y})
    model = fit_gaussian_copula(frame, snapshot(frame, numeric_bins=4), seed=seed_bytes(1))
    # Latent order: a, flag, y, y-null. The flag tells apart only `shared` of y's 60 rows.
    assert _suppressed(model) == ({(1, "distinguished_rows")} if suppressed else set())
    if suppressed:
        assert np.all(np.delete(model.correlation_matrix()[1], 1) == 0)


@pytest.mark.parametrize(("shared", "suppressed"), [(1, True), (11, False)])
def test_rare_category_on_shared_rows(shared: int, suppressed: bool) -> None:
    rng = np.random.default_rng(30)
    flag, y = _sparse_partner(rng, 1000, shared)
    labels = np.where(flag == 1, "rare", "common").astype(object)
    frame = pd.DataFrame({"a": rng.standard_normal(1000), "k": labels, "y": y})
    model = fit_gaussian_copula(frame, snapshot(frame, numeric_bins=4), seed=seed_bytes(1))
    assert _suppressed(model) == ({(1, "distinguished_rows")} if suppressed else set())


@pytest.mark.parametrize(("shared", "suppressed"), [(1, True), (11, False)])
def test_null_indicator_on_shared_rows(shared: int, suppressed: bool) -> None:
    rng = np.random.default_rng(31)
    flag, y = _sparse_partner(rng, 1000, shared, observed=80)
    z = rng.standard_normal(1000)
    z[flag == 1] = np.nan  # z is null on 30 rows, `shared` of them where y is observed
    frame = pd.DataFrame({"z": z, "y": y})
    model = fit_gaussian_copula(frame, snapshot(frame, numeric_bins=4), seed=seed_bytes(1))
    # Latent order: z, z-null, y, y-null.
    assert _suppressed(model) == ({(1, "distinguished_rows")} if suppressed else set())


def test_structural_zero_holds_when_the_raw_matrix_is_indefinite() -> None:
    rng = np.random.default_rng(32)
    x = rng.standard_normal(2000)
    y = x + 0.05 * rng.standard_normal(2000)
    y[x > 0] = np.nan  # missing exactly where x is high: the indicator tracks x as well
    frame = pd.DataFrame({"x": x, "y": y})
    snap = snapshot(frame)
    columns = tuple(from_snapshot_entry(c, snap["columns"][c]) for c in frame.columns)
    layout = latent_layout(columns)
    stats = _fit._PairStats(_fit.latent_scores(frame, columns, layout, fit_stream(seed_bytes(1))))
    raw = _fit.raw_correlation(stats, layout)
    assert min_eigenvalue(raw) < 0  # holding y and its indicator at 0 is inconsistent with x
    model = fit_gaussian_copula(frame, snap, seed=seed_bytes(1))
    matrix = model.correlation_matrix()
    assert matrix[1, 2] == 0.0 and abs(matrix[0, 1]) > 0.5 and abs(matrix[0, 2]) > 0.5


@pytest.mark.parametrize("floor", [1e-8, 1e-3, 0.01, 0.1])
def test_repair_reaches_the_requested_floor(floor: float) -> None:
    for case in range(20):
        rng = np.random.default_rng(100 + case)
        a = rng.uniform(-1, 1, (8, 8))
        a = (a + a.T) / 2
        np.fill_diagonal(a, 1.0)
        out = nearest_correlation(a, floor=floor)
        assert min_eigenvalue(out) >= floor
        assert np.all(np.diag(out) == 1.0)


def test_shared_row_tie_suppresses_the_later_variable() -> None:
    rows = 1000
    a = np.full(rows, np.nan)
    b = np.full(rows, np.nan)
    a[:100] = 0.0
    b[40:140] = 0.0
    a[40:45] = 1.0  # 5 ones where both are observed (rows 40 to 99)
    a[0:15] = 1.0
    b[45:50] = 1.0  # 5 ones where both are observed
    b[100:115] = 1.0
    frame = pd.DataFrame({"a": a, "b": b})
    model = fit_gaussian_copula(frame, snapshot(frame, numeric_bins=4), seed=seed_bytes(1))
    # Latent order: a, a-null, b, b-null; each tells apart 5 of the 60 shared rows.
    assert _suppressed(model) == {(2, "distinguished_rows")}
