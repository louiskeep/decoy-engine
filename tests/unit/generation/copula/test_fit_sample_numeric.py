"""A3: numeric columns keep their pairwise dependence and their snapshot marginals.

Bounds come from the plan's 50-seed sweep (section 8), each below the observed minimum; the
independent baseline (same marginals, columns permuted apart) must fail the Spearman bound.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula import fit_gaussian_copula, sample_gaussian_copula

from ._fixtures import PINNED, independent, numeric_frame, seed_bytes, snapshot
from ._pairwise_fidelity import correlation_similarity

SPEARMAN_MIN = 0.97
PEARSON_MIN = 0.90
BIN_PROPORTION_MAX = 0.03
_PAIRS = [("x", "y"), ("x", "w"), ("y", "w")]


def _fit_and_sample(i: int, frame: pd.DataFrame) -> tuple[object, pd.DataFrame]:
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(2000 + i))
    return model, sample_gaussian_copula(model, len(frame), seed=seed_bytes(3000 + i))


@pytest.mark.parametrize("i", PINNED)
def test_pairwise_correlation_fidelity(i: int) -> None:
    frame = numeric_frame(i)
    _, synth = _fit_and_sample(i, frame)
    baseline = independent(synth, i)
    for a, b in _PAIRS:
        assert correlation_similarity(frame, synth, a, b, "spearman") >= SPEARMAN_MIN, (a, b)
        assert correlation_similarity(frame, synth, a, b, "pearson") >= PEARSON_MIN, (a, b)
        assert correlation_similarity(frame, baseline, a, b, "spearman") < SPEARMAN_MIN, (a, b)


@pytest.mark.parametrize("i", PINNED)
def test_marginals_follow_the_snapshot_histogram(i: int) -> None:
    frame = numeric_frame(i)
    model, synth = _fit_and_sample(i, frame)
    for column in model.columns:
        edges = np.asarray(column.bin_edges)
        expected = np.asarray(column.bin_counts) / column.non_null_count
        values = synth[column.name].to_numpy(dtype=float)
        got, _ = np.histogram(np.clip(values, edges[0], edges[-1]), bins=edges)
        assert np.max(np.abs(got / len(values) - expected)) <= BIN_PROPORTION_MAX, column.name


def test_integer_column_stays_integer_and_in_range() -> None:
    frame = numeric_frame(0)
    model, synth = _fit_and_sample(0, frame)
    w = next(c for c in model.columns if c.name == "w")
    assert synth["w"].dtype == np.int64
    assert synth["w"].min() >= w.bin_edges[0] and synth["w"].max() <= w.bin_edges[-1]
    assert synth["x"].dtype == np.float64


def test_perfect_duplicate_column_is_repaired_and_kept() -> None:
    rng = np.random.default_rng(5)
    a = rng.standard_normal(500)
    frame = pd.DataFrame({"a": a, "b": 2 * a})
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    synth = sample_gaussian_copula(model, 500, seed=seed_bytes(2))
    assert correlation_similarity(frame, synth, "a", "b", "spearman") >= 0.99
    assert model.suppressed == ()


def test_constant_column_samples_its_constant() -> None:
    rng = np.random.default_rng(6)
    frame = pd.DataFrame({"a": rng.standard_normal(300), "k": np.full(300, 4.5)})
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    synth = sample_gaussian_copula(model, 50, seed=seed_bytes(2))
    assert (synth["k"] == 4.5).all()
