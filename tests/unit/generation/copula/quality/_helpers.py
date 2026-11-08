"""Shared builders for the copula quality report tests."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.generation.copula import (
    CopulaQualityReport,
    GaussianCopulaModel,
    PairScore,
    compute_copula_quality_report,
    fit_gaussian_copula,
    sample_gaussian_copula,
)
from decoy_engine.quality.snapshot import compute_distribution_snapshot

FIT_SEED = bytes(range(16))
SAMPLE_SEED = bytes(range(1, 17))


def fit(frame: pd.DataFrame, **snapshot_kwargs: Any) -> GaussianCopulaModel:
    snapshot = compute_distribution_snapshot(frame, **snapshot_kwargs)
    return fit_gaussian_copula(frame, snapshot, seed=FIT_SEED)


def sample(model: GaussianCopulaModel, n: int | None = None) -> pd.DataFrame:
    return sample_gaussian_copula(model, model.row_count if n is None else n, seed=SAMPLE_SEED)


def report(
    real: pd.DataFrame,
    model: GaussianCopulaModel,
    synthetic: pd.DataFrame | None = None,
    **kwargs: Any,
) -> CopulaQualityReport:
    synth = sample(model) if synthetic is None else synthetic
    return compute_copula_quality_report(real, synth, model, **kwargs)


def pair(result: CopulaQualityReport, a: str, b: str) -> PairScore:
    return next(p for p in result.pairs if set(p.columns) == {a, b})


def correlated(n: int, rho: float, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n, 2)) @ np.linalg.cholesky([[1.0, rho], [rho, 1.0]]).T
    return z[:, 0], z[:, 1]
