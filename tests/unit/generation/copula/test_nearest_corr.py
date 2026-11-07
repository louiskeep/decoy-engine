"""A2: Higham's nearest correlation matrix and the shrink-to-definite backstop."""

from __future__ import annotations

import numpy as np
import pytest

from decoy_engine.generation.copula._nearest_corr import (
    SHRINK_LADDER,
    min_eigenvalue,
    nearest_correlation,
    shrink_to_definite,
)


def test_higham_2002_published_example() -> None:
    out = nearest_correlation([[1, 1, 0], [1, 1, 1], [0, 1, 1]])
    assert round(out[0, 1], 4) == 0.7607
    assert round(out[1, 2], 4) == 0.7607
    assert round(out[0, 2], 4) == 0.1573


def _random_indefinite(rng: np.random.Generator, size: int) -> np.ndarray:
    while True:
        a = rng.uniform(-1, 1, (size, size))
        a = (a + a.T) / 2
        np.fill_diagonal(a, 1.0)
        if np.linalg.eigvalsh(a)[0] < 0:
            return a


def _clip_and_rescale(a: np.ndarray) -> np.ndarray:
    values, vectors = np.linalg.eigh(a)
    x = (vectors * np.maximum(values, 1e-8)) @ vectors.T
    d = 1 / np.sqrt(np.diag(x))
    return x * d[:, None] * d[None, :]


@pytest.mark.parametrize("case", range(50))
def test_random_indefinite_inputs_become_valid_and_near(case: int) -> None:
    rng = np.random.default_rng(case)
    a = _random_indefinite(rng, 3 + case % 28)
    out = nearest_correlation(a)
    assert np.allclose(out, out.T, atol=0)
    assert np.all(np.diag(out) == 1.0)
    assert min_eigenvalue(out) >= 5e-9
    np.linalg.cholesky(out)
    assert np.linalg.norm(out - a) <= np.linalg.norm(_clip_and_rescale(a) - a) + 1e-9


def test_a_valid_matrix_is_returned_unchanged() -> None:
    a = np.array([[1.0, 0.3, -0.2], [0.3, 1.0, 0.5], [-0.2, 0.5, 1.0]])
    assert np.max(np.abs(nearest_correlation(a) - a)) <= 1e-12


def test_iteration_cap_still_returns_a_valid_matrix() -> None:
    a = _random_indefinite(np.random.default_rng(7), 12)
    out = nearest_correlation(a, max_iterations=1)
    assert np.all(np.diag(out) == 1.0) and min_eigenvalue(out) >= 5e-9


def test_floor_is_honoured() -> None:
    a = np.array([[1.0, 1.0], [1.0, 1.0]])
    out = nearest_correlation(a, floor=0.01)
    assert min_eigenvalue(out) >= 0.01 / (1 + 0.01) - 1e-12


def test_shrink_picks_the_smallest_ladder_step() -> None:
    near_singular = np.array([[1.0, 0.9996], [0.9996, 1.0]])

    def finish(m: np.ndarray) -> np.ndarray:
        return np.round(m, 3)

    out = shrink_to_definite(near_singular, finish, 1e-6)
    assert min_eigenvalue(out) >= 1e-6
    # Rounding alone gives 1.0 (singular); the first ladder step that rounds below 1 is used.
    firsts = [
        a
        for a in SHRINK_LADDER
        if min_eigenvalue(finish((1 - a) * near_singular + a * np.eye(2))) >= 1e-6
    ]
    assert out[0, 1] == round((1 - firsts[0]) * 0.9996, 3)


def test_rejects_non_square_and_non_finite() -> None:
    with pytest.raises(ValueError, match="square"):
        nearest_correlation(np.ones((2, 3)))
    with pytest.raises(ValueError, match="non-finite"):
        nearest_correlation(np.array([[1.0, np.nan], [np.nan, 1.0]]))
