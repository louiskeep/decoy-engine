"""A4: categorical, null and mixed columns keep their frequencies and their dependence.

Bounds come from the plan's 50-seed sweep (section 8); the independent baseline has the same
marginals with every column permuted apart.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula import _fit, fit_gaussian_copula, sample_gaussian_copula
from decoy_engine.generation.copula._marginals import from_snapshot_entry
from decoy_engine.generation.copula._model import latent_layout

from ._fixtures import PINNED, fit_stream, independent, mixed_frame, seed_bytes, snapshot
from ._pairwise_fidelity import contingency_similarity, correlation_similarity

SPEARMAN_MIN = 0.97
PEARSON_MIN = 0.90
FREQUENCY_MAX = 0.03
NULL_RATE_MAX = 0.025
PAIR_GAP_MIN = -0.03
MEAN_GAP_MIN = 0.015
_NUMERIC = ["x", "y", "w"]
_CATEGORICAL = ["c", "f", "g"]


def _fit_and_sample(i: int, frame: pd.DataFrame):  # type: ignore[no-untyped-def]
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(2000 + i))
    return model, sample_gaussian_copula(model, len(frame), seed=seed_bytes(3000 + i))


@pytest.mark.parametrize("i", PINNED)
def test_mixed_fidelity(i: int) -> None:
    frame = mixed_frame(i)
    _, synth = _fit_and_sample(i, frame)
    baseline = independent(synth, i)
    for a, b in [("x", "y"), ("x", "w"), ("y", "w")]:
        assert correlation_similarity(frame, synth, a, b, "spearman") >= SPEARMAN_MIN, (a, b)
        assert correlation_similarity(frame, synth, a, b, "pearson") >= PEARSON_MIN, (a, b)
        assert correlation_similarity(frame, baseline, a, b, "spearman") < SPEARMAN_MIN, (a, b)
    columns = _NUMERIC + _CATEGORICAL
    pairs = [
        (a, b)
        for k, a in enumerate(columns)
        for b in columns[k + 1 :]
        if a in _CATEGORICAL or b in _CATEGORICAL
    ]
    copula = np.array([contingency_similarity(frame, synth, a, b) for a, b in pairs])
    indep = np.array([contingency_similarity(frame, baseline, a, b) for a, b in pairs])
    assert np.min(copula - indep) >= PAIR_GAP_MIN
    assert copula.mean() - indep.mean() >= MEAN_GAP_MIN


@pytest.mark.parametrize("i", PINNED)
def test_frequencies_and_null_rates(i: int) -> None:
    frame = mixed_frame(i)
    _, synth = _fit_and_sample(i, frame)
    for name in _CATEGORICAL:
        real = frame[name].fillna("<null>").value_counts(normalize=True)
        got = synth[name].fillna("<null>").value_counts(normalize=True)
        assert real.sub(got, fill_value=0).abs().max() <= FREQUENCY_MAX, name
    assert abs(frame["w"].isna().mean() - synth["w"].isna().mean()) <= NULL_RATE_MAX
    assert str(synth["w"].dtype) == "Int64"


def test_collapsed_tail_is_redistributed_and_never_emitted() -> None:
    rng = np.random.default_rng(3)
    labels = np.array([f"L{k:02d}" for k in range(25)], dtype=object)
    weights = np.linspace(2.0, 1.0, 25)
    frame = pd.DataFrame(
        {"v": rng.standard_normal(4000), "k": rng.choice(labels, 4000, p=weights / weights.sum())}
    )
    snap = snapshot(frame)
    model = fit_gaussian_copula(frame, snap, seed=seed_bytes(1))
    synth = sample_gaussian_copula(model, 20000, seed=seed_bytes(2))
    kept = {e["value"]: e["count"] for e in snap["columns"]["k"]["stats"]["top_values"]}
    assert len(kept) == 20 and set(synth["k"].unique()) <= set(kept)
    expected = pd.Series(kept, dtype=float) / sum(kept.values())
    got = synth["k"].value_counts(normalize=True)
    assert expected.sub(got, fill_value=0).abs().max() <= FREQUENCY_MAX


def test_all_null_and_bool_columns() -> None:
    rng = np.random.default_rng(4)
    frame = pd.DataFrame(
        {"v": rng.standard_normal(300), "flag": rng.random(300) > 0.4, "none": [None] * 300}
    )
    model = fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1))
    synth = sample_gaussian_copula(model, 200, seed=seed_bytes(2))
    assert synth["none"].isna().all()
    assert set(synth["flag"].unique()) <= {"True", "False"}


def test_interval_latents_are_normal_scores() -> None:
    """A binary column cut from a bivariate normal: its latent is a normal score, so the fitted
    correlation equals the normal-score estimate, which is clearly apart from the estimate on the
    raw interval uniforms (the cell would move if fit correlated the uniforms instead)."""
    rng = np.random.default_rng(8)
    n = 20000
    z = rng.standard_normal((n, 2)) @ np.linalg.cholesky(np.array([[1, 0.7], [0.7, 1]])).T
    frame = pd.DataFrame({"v": z[:, 0], "b": np.where(z[:, 1] > 0, "hi", "lo").astype(object)})
    snap = snapshot(frame)
    columns = tuple(from_snapshot_entry(c, snap["columns"][c]) for c in frame.columns)
    layout = latent_layout(columns)
    scores = _fit.latent_scores(frame, columns, layout, fit_stream(seed_bytes(1))).scores
    assert abs(np.mean(scores[:, 1])) < 0.05 and abs(np.std(scores[:, 1]) - 1.0) < 0.05
    normal_score = abs(np.corrcoef(scores[:, 0], scores[:, 1])[0, 1])
    from decoy_engine.generation.copula._normal import ndtr

    raw_uniform = abs(np.corrcoef(scores[:, 0], ndtr(scores[:, 1]))[0, 1])
    model = fit_gaussian_copula(frame, snap, seed=seed_bytes(1))
    assert abs(abs(model.correlation[0]) - round(normal_score, 3)) <= 0.0015
    assert abs(normal_score - raw_uniform) > 0.01
