"""Nearest correlation matrix and definiteness repair.

Pattern: Higham nearest correlation matrix (Higham, "Computing the nearest correlation matrix: a
problem from finance", IMA J. Numer. Anal. 22, 2002, Algorithm 3.3 with W = I). See:
https://nhigham.com/2013/02/13/the-nearest-correlation-matrix/.

Alternating projections between the positive semidefinite cone and the unit-diagonal set, with
Dykstra's correction on the semidefinite step (without it the iteration converges to a point that
is not the nearest one). After convergence, or when the iteration cap is hit (not an error), the
eigenvalues are floored and the matrix is rescaled to unit diagonal (`D^-1/2 X D^-1/2`, a
congruence, so it stays positive definite), as statsmodels `corr_clipped` does.

`shrink_to_definite` restores a minimum eigenvalue after rounding by blending toward the identity,
`(1 - a) C + a I`, with the smallest `a` from a fixed ladder (Higham, Strabic, Sego, "Restoring
definiteness via shrinking", SIAM Review 58, 2016).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

_TOLERANCE = 1e-12
_MAX_ITERATIONS = 500
_DEFAULT_FLOOR = 1e-8
SHRINK_LADDER = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0)


def _psd_projection(matrix: FloatArray, floor: float) -> FloatArray:
    values, vectors = np.linalg.eigh(matrix)
    clipped = np.maximum(values, floor)
    out: FloatArray = (vectors * clipped) @ vectors.T
    return (out + out.T) * 0.5


def nearest_correlation(
    matrix: npt.ArrayLike,
    *,
    floor: float = _DEFAULT_FLOOR,
    max_iterations: int = _MAX_ITERATIONS,
) -> FloatArray:
    """Nearest correlation matrix in Frobenius norm, then floored to minimum eigenvalue `floor`."""
    a = np.array(matrix, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] != a.shape[1]:
        raise ValueError("nearest_correlation: expected a square matrix.")
    if not np.all(np.isfinite(a)):
        raise ValueError("nearest_correlation: the matrix has a non-finite entry.")
    a = (a + a.T) * 0.5
    if a.shape[0] == 0:
        return a
    y = a.copy()
    np.fill_diagonal(y, 1.0)
    correction = np.zeros_like(a)
    for _ in range(max_iterations):
        r = y - correction
        x = _psd_projection(r, 0.0)
        correction = x - r
        y_next = x.copy()
        np.fill_diagonal(y_next, 1.0)
        change = float(np.linalg.norm(y_next - y)) / max(float(np.linalg.norm(y)), 1.0)
        y = y_next
        if change < _TOLERANCE:
            break
    return _floor_and_rescale(y, floor)


def _floor_and_rescale(matrix: FloatArray, floor: float) -> FloatArray:
    x = _psd_projection(matrix, floor)
    scale = 1.0 / np.sqrt(np.diag(x))
    out: FloatArray = x * scale[:, None] * scale[None, :]
    out = (out + out.T) * 0.5
    np.fill_diagonal(out, 1.0)
    return out


def min_eigenvalue(matrix: FloatArray) -> float:
    if matrix.shape[0] == 0:
        return 1.0
    return float(np.linalg.eigvalsh(matrix)[0])


def shrink_to_definite(
    matrix: FloatArray,
    finish: Callable[[FloatArray], FloatArray],
    minimum: float,
) -> FloatArray:
    """`finish(matrix)` if its minimum eigenvalue reaches `minimum`, else the first ladder blend
    toward the identity whose finished form does. `finish` is the rounding the release applies."""
    candidate = finish(matrix)
    if min_eigenvalue(candidate) >= minimum:
        return candidate
    identity = np.eye(matrix.shape[0])
    for a in SHRINK_LADDER:
        candidate = finish((1.0 - a) * matrix + a * identity)
        if min_eigenvalue(candidate) >= minimum:
            return candidate
    return candidate  # a = 1 is the identity, which always qualifies
