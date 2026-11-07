"""Gaussian copula synthesis: synthetic rows that keep pairwise dependence between columns.

`fit_gaussian_copula(frame, snapshot, seed=...)` takes the frame's `distribution-snapshot/v1` for
the marginals and computes a released latent correlation matrix under disclosure floors;
`sample_gaussian_copula(model, n, seed=..., start=...)` draws rows from the model alone. Library
only: no config key or strategy reaches this package yet, and the model is not differentially
private. It holds the snapshot's real bin edges and category labels, so treat it like the exact
snapshot.
"""

from decoy_engine.generation.copula._fit import fit_gaussian_copula
from decoy_engine.generation.copula._floors import (
    MAX_LATENT,
    MIN_DISTINGUISHED_ROWS,
    MIN_FIT_ROWS,
    MIN_PAIR_ROWS,
    ROWS_PER_BIN,
)
from decoy_engine.generation.copula._model import (
    COPULA_MODEL_SCHEMA_VERSION,
    ColumnMarginal,
    GaussianCopulaModel,
    Suppression,
)
from decoy_engine.generation.copula._sample import sample_gaussian_copula

__all__ = [
    "COPULA_MODEL_SCHEMA_VERSION",
    "MAX_LATENT",
    "MIN_DISTINGUISHED_ROWS",
    "MIN_FIT_ROWS",
    "MIN_PAIR_ROWS",
    "ROWS_PER_BIN",
    "ColumnMarginal",
    "GaussianCopulaModel",
    "Suppression",
    "fit_gaussian_copula",
    "sample_gaussian_copula",
]
