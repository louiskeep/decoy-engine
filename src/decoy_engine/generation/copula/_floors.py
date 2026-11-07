"""Disclosure floors for the copula's correlation release, in one place.

The snapshot alone never links values within a row; the correlation does, and with few rows it is
close to the rows themselves. These are heuristic small-cell controls (CMS cell size suppression
policy, minimum 11; Sweeney's k-anonymity minimum group), not a formal guarantee; the reasoning
for each number is in docs/plans/2026-11-07-v2-4.2-4.3-gaussian-copula.md section 4.2.
"""

from __future__ import annotations

MIN_FIT_ROWS = 100
MIN_PAIR_ROWS = 50
MIN_DISTINGUISHED_ROWS = 11
ROWS_PER_BIN = 10
MAX_LATENT = 256
RELEASE_DECIMALS = 3
MIN_EIGENVALUE = 1e-6

REASON_PAIR_ROWS = "pair_rows"
REASON_DISTINGUISHED_ROWS = "distinguished_rows"
REASON_ROWS_PER_BIN = "rows_per_bin"
REASON_ZERO_VARIANCE = "zero_variance"
SUPPRESSION_REASONS = frozenset(
    {REASON_PAIR_ROWS, REASON_DISTINGUISHED_ROWS, REASON_ROWS_PER_BIN, REASON_ZERO_VARIANCE}
)


def max_latent_pairs(row_count: int) -> int:
    """Never release more correlations than half the row count (a heuristic budget)."""
    return row_count // 2


def rounding_floor(dimension: int) -> float:
    """Eigenvalue floor that survives rounding every off-diagonal entry to RELEASE_DECIMALS.

    The rounding error matrix has Frobenius norm at most half a unit in the last kept place times
    sqrt(d (d - 1)), which bounds how far any eigenvalue can move.
    """
    half_unit = 0.5 * 10.0**-RELEASE_DECIMALS
    # Twice the minimum leaves room for the unit-diagonal rescale that follows the floor.
    return 2 * MIN_EIGENVALUE + half_unit * (dimension * (dimension - 1)) ** 0.5
