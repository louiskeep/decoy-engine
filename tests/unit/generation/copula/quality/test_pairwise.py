"""A1 and A2: the SDMetrics definitions, checked against independent oracles and on copula output."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula import compute_copula_quality_report
from decoy_engine.quality.fidelity import compute_fidelity
from decoy_engine.quality.snapshot import compute_distribution_snapshot

from .._fixtures import PINNED, independent, mixed_frame, numeric_frame, seed_bytes, snapshot
from .._pairwise_fidelity import pairwise_fidelity
from ._helpers import fit, pair, report, sample

_SPEARMAN = "spearman_correlation_similarity"
_CONTINGENCY = "contingency_similarity"


def _two_categoricals(cells: dict[tuple[str, str], int]) -> pd.DataFrame:
    rows = [key for key, count in cells.items() for _ in range(count)]
    return pd.DataFrame({"c": [r[0] for r in rows], "d": [r[1] for r in rows]}, dtype=object)


def test_identical_frames_score_one() -> None:
    real = mixed_frame(0, n=2000)
    model = fit(real)
    result = compute_copula_quality_report(real, real.copy(), model)
    scored = [p for p in result.pairs if p.status == "scored"]
    assert scored and all(p.score == 1.0 for p in scored)
    assert result.pairwise_score == 1.0


def test_contingency_hand_computed() -> None:
    real = _two_categoricals({("a", "u"): 70, ("a", "v"): 50, ("b", "u"): 30, ("b", "v"): 50})
    synth = _two_categoricals({("a", "u"): 60, ("a", "v"): 60, ("b", "u"): 40, ("b", "v"): 40})
    result = report(real, fit(real), synth)
    # Each cell differs by 10 of 200 rows: TVD = 0.5 * 4 * 0.05 = 0.1.
    assert pair(result, "c", "d").score == 0.9
    assert pair(result, "c", "d").metric == _CONTINGENCY


def test_nulls_are_their_own_cell() -> None:
    real = _two_categoricals({("a", "u"): 60, ("a", "v"): 40, ("b", "u"): 40, ("b", "v"): 60})
    real.loc[:19, "c"] = None  # 20 nulls, all paired with "u"
    synth = real.copy()
    synth.loc[:19, "d"] = "v"  # move the null cell's mass from (null, u) to (null, v)
    result = report(real, fit(real), synth)
    # Only (null, u) and (null, v) differ, by 20 rows each: TVD = 0.5 * 40 / 200.
    assert pair(result, "c", "d").score == 0.9


def test_spearman_matches_pandas_with_ties_and_nulls() -> None:
    rng = np.random.default_rng(7)
    n = 400
    x = np.round(rng.normal(size=n) * 3)  # heavy ties
    y = x + rng.normal(size=n)
    real = pd.DataFrame({"x": x, "y": y})
    real.loc[rng.random(n) < 0.1, "y"] = np.nan
    model = fit(real, numeric_bins=5)
    synth = sample(model)
    result = report(real, model, synth)

    def rho(frame: pd.DataFrame) -> float:
        both = frame[["x", "y"]].dropna()
        return float(both["x"].corr(both["y"], method="spearman"))

    expected = round(1 - abs(rho(real) - rho(synth)) / 2, 3)
    assert pair(result, "x", "y").metric == _SPEARMAN
    assert pair(result, "x", "y").score == expected


def _no_small_states() -> pd.DataFrame:
    """Correlated columns whose every discrete state holds well over 11 rows: uniform marginals
    (10 equal bins of about 500 rows), three- and two-state categoricals, 5% nulls."""
    from decoy_engine.generation.copula._normal import ndtr

    rng = np.random.default_rng(70)
    z = (
        rng.standard_normal((5000, 4))
        @ np.linalg.cholesky(
            [[1, 0.7, -0.4, 0.5], [0.7, 1, -0.3, 0.4], [-0.4, -0.3, 1, 0], [0.5, 0.4, 0, 1]]
        ).T
    )
    u = ndtr(z)
    frame = pd.DataFrame(
        {
            "x": u[:, 0],
            "y": u[:, 1] * 100,
            "w": u[:, 2] * 10,
            "c": np.where(u[:, 3] < 0.3, "a", np.where(u[:, 3] < 0.7, "b", "c")).astype(object),
            "f": np.where(u[:, 0] + 0.3 * rng.random(5000) > 0.6, "yes", "no").astype(object),
        }
    )
    frame.loc[rng.random(5000) < 0.05, "w"] = np.nan
    frame.loc[rng.random(5000) < 0.05, "c"] = None
    return frame


def test_matches_the_copula_test_helper() -> None:
    from decoy_engine.generation.copula._quality_inputs import _raw_states, canonical

    real = _no_small_states()
    model = fit(real)
    synth = sample(model)
    canon = canonical(real, model)
    for column in model.columns:
        counts = np.bincount(_raw_states(canon[column.name], column)[0])
        assert all(c == 0 or c >= 11 for c in counts), column.name  # nothing is recoded
    result = report(real, model, synth)
    helper = {(p.a, p.b, p.metric): p.score for p in pairwise_fidelity(real, synth)}
    for p in result.pairs:
        assert p.status == "scored"
        if p.metric == _SPEARMAN:
            expected = helper[(p.columns[0], p.columns[1], "spearman")]
            assert expected is not None and p.score == round(expected, 3)
        else:
            expected = helper[(p.columns[0], p.columns[1], "contingency")]
            assert p.score == pytest.approx(expected, abs=0.0005)  # rounding only
    assert len(result.pairs) == 10


@pytest.mark.parametrize("i", PINNED)
def test_numeric_fidelity_on_copula_output(i: int) -> None:
    from decoy_engine.generation.copula import fit_gaussian_copula, sample_gaussian_copula

    real = numeric_frame(i)
    model = fit_gaussian_copula(real, snapshot(real), seed=seed_bytes(2000 + i))
    synth = sample_gaussian_copula(model, len(real), seed=seed_bytes(3000 + i))
    good = compute_copula_quality_report(real, synth, model)
    base = compute_copula_quality_report(real, independent(synth, i), model)
    assert all(p.score is not None and p.score >= 0.97 for p in good.pairs)
    assert any(p.score is not None and p.score < 0.97 for p in base.pairs)
    assert base.correlation_score is not None and good.correlation_score is not None
    assert base.correlation_score < good.correlation_score


@pytest.mark.parametrize("i", PINNED)
def test_mixed_fidelity_on_copula_output(i: int) -> None:
    from decoy_engine.generation.copula import fit_gaussian_copula, sample_gaussian_copula

    real = mixed_frame(i)
    model = fit_gaussian_copula(real, snapshot(real), seed=seed_bytes(2000 + i))
    synth = sample_gaussian_copula(model, len(real), seed=seed_bytes(3000 + i))
    good = compute_copula_quality_report(real, synth, model)
    base = compute_copula_quality_report(real, independent(synth, i), model)
    for p in good.pairs:
        if p.metric == _SPEARMAN:
            assert p.score is not None and p.score >= 0.97
    gaps = []
    for p, q in zip(good.pairs, base.pairs, strict=True):
        if p.metric == _CONTINGENCY:
            assert p.score is not None and q.score is not None
            assert p.score - q.score >= -0.03
            gaps.append(p.score - q.score)
    assert sum(gaps) / len(gaps) >= 0.015


def test_marginal_entries_are_the_fidelity_extras() -> None:
    real = mixed_frame(17, n=2000)
    model = fit(real)
    synth = sample(model)
    result = report(real, model, synth)
    fidelity = compute_fidelity(
        compute_distribution_snapshot(real), compute_distribution_snapshot(synth)
    )
    extras = {e["column"]: e["extra_metrics"] for e in fidelity["marginal"]["columns"]}
    for entry in result.marginal:
        gof = next(iter(extras[entry.column].values()))
        assert entry.gof_method == gof["method"]
        assert entry.gof_value == round(gof["value"], 3)
