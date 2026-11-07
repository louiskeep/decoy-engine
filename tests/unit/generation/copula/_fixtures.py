"""Fixtures from the plan's section 8 sweep: correlated latent normals pushed through skewed,
heavy-tailed and integer marginals, plus categorical and null columns for the mixed case."""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine.quality.snapshot import compute_distribution_snapshot

N = 5000
PINNED = (0, 17, 42)
LATENT = np.array([[1, 0.8, -0.5, 0.6], [0.8, 1, -0.4, 0.5], [-0.5, -0.4, 1, 0], [0.6, 0.5, 0, 1]])


def seed_bytes(value: int) -> bytes:
    return value.to_bytes(16, "big")


def numeric_frame(i: int, n: int = N) -> pd.DataFrame:
    rng = np.random.default_rng(1000 + i)
    z = rng.standard_normal((n, 4)) @ np.linalg.cholesky(LATENT).T
    return pd.DataFrame(
        {
            "x": np.exp(z[:, 0]),
            "y": z[:, 1] ** 3 + z[:, 1],
            "w": np.round(50 + 10 * z[:, 2]).astype(np.int64),
        }
    )


def mixed_frame(i: int, n: int = N) -> pd.DataFrame:
    rng = np.random.default_rng(1000 + i)
    z = rng.standard_normal((n, 4)) @ np.linalg.cholesky(LATENT).T
    c = np.where(z[:, 3] < 0.0, "a", np.where(z[:, 3] < 0.9, "b", "c")).astype(object)
    f = np.where(z[:, 0] + 0.5 * rng.standard_normal(n) > 0.4, "yes", "no").astype(object)
    g = np.where(z[:, 3] + 0.5 * rng.standard_normal(n) > 0.2, "hi", "lo").astype(object)
    w = pd.array(np.round(50 + 10 * z[:, 2]).astype(np.int64), dtype="Int64")
    frame = pd.DataFrame(
        {"x": np.exp(z[:, 0]), "y": z[:, 1] ** 3 + z[:, 1], "w": w, "c": c, "f": f, "g": g}
    )
    frame.loc[z[:, 2] + rng.standard_normal(n) > 1.5, "w"] = pd.NA
    frame.loc[rng.random(n) < 0.05, "c"] = None
    return frame


def snapshot(frame: pd.DataFrame, **kwargs: object) -> dict:
    return compute_distribution_snapshot(frame, **kwargs)  # type: ignore[arg-type]


def independent(frame: pd.DataFrame, i: int) -> pd.DataFrame:
    """Same marginals, no dependence: each column permuted on its own (what `statistical` gives)."""
    rng = np.random.default_rng(4000 + i)
    return pd.DataFrame(
        {c: frame[c].to_numpy()[rng.permutation(len(frame))] for c in frame.columns}
    )


def fit_stream(seed: bytes) -> np.random.PCG64:
    """The stream `fit_gaussian_copula` derives from `seed` (same expression, for white-box tests)."""
    import hashlib

    from decoy_engine.generation.copula import _rng

    digest = hashlib.sha256(_rng.framed(_rng.FIT_LABEL) + len(seed).to_bytes(4, "big") + seed)
    return np.random.PCG64(np.random.SeedSequence(int.from_bytes(digest.digest(), "big")))
