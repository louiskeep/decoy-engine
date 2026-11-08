"""A3: the release gate. Each floor cell asserts status, reason and that no score is released."""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine.generation.copula._quality_pairwise import spearman

from ._helpers import correlated, fit, pair, report, sample


def _withheld(result, a: str, b: str, reason: str) -> None:  # type: ignore[no-untyped-def]
    p = pair(result, a, b)
    assert (p.status, p.reason, p.score) == ("withheld", reason, None)


def _scored(result, a: str, b: str) -> None:  # type: ignore[no-untyped-def]
    p = pair(result, a, b)
    assert p.status == "scored" and p.reason is None and p.score is not None


def test_suppressed_variable_pair_is_withheld() -> None:
    # 150 values in 20 bins: both value variables suppressed for rows_per_bin, so only R1 applies.
    x, y = correlated(150, 0.9, seed=1)
    real = pd.DataFrame({"x": x, "y": y})
    model = fit(real, numeric_bins=20)
    assert {s.reason for s in model.suppressed} == {"rows_per_bin"}
    synth = sample(model)
    result = report(real, model, synth)
    _withheld(result, "x", "y", "suppressed_in_model")
    assert result.pairwise_score is None
    # The leak the gate closes: ungated, the score gives back the withheld real coefficient.
    rho_real, rho_synth = spearman(real["x"], real["y"]), spearman(synth["x"], synth["y"])
    assert rho_real is not None and rho_synth is not None and abs(rho_real) > 0.8
    ungated = 1 - abs(rho_real - rho_synth) / 2
    assert abs(2 * (1 - ungated) - abs(rho_real)) <= abs(rho_synth) + 1e-12


def test_suppressed_null_indicator_withholds_contingency_not_spearman() -> None:
    rng = np.random.default_rng(2)
    x, w = correlated(300, 0.6, seed=2)
    c = np.where(x < -0.4, "lo", np.where(x < 0.4, "mid", "hi")).astype(object)
    real = pd.DataFrame({"x": x, "w": w, "c": c})
    real.loc[rng.choice(300, 3, replace=False), "w"] = np.nan
    model = fit(real)
    assert any(s.reason == "distinguished_rows" for s in model.suppressed)
    result = report(real, model)
    _scored(result, "w", "x")
    _withheld(result, "w", "c", "suppressed_in_model")
    _scored(result, "x", "c")


def _overlap_frames(overlap: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fit frame: a and b observed on rows 0..59. Report frame: b's 60 values moved so the two
    share `overlap` rows. Marginals are unchanged, so the model's facts still hold."""
    a, b = correlated(60, 0.7, seed=3)
    fit_frame = pd.DataFrame({"a": np.full(100, np.nan), "b": np.full(100, np.nan)})
    fit_frame.loc[:59, "a"] = a
    fit_frame.loc[:59, "b"] = b
    moved = fit_frame.copy()
    moved["b"] = np.nan
    start = 60 - overlap
    moved.loc[start : start + 59, "b"] = b
    return fit_frame, moved


def test_pair_rows_floor_on_the_frame_passed_in() -> None:
    fit_frame, moved = _overlap_frames(49)
    model = fit(fit_frame, numeric_bins=5)
    _withheld(report(moved, model), "a", "b", "pair_rows")
    fit_frame, moved = _overlap_frames(50)
    model = fit(fit_frame, numeric_bins=5)
    _scored(report(moved, model), "a", "b")


def _flag_frames(ones_seen: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A 0/1 flag with 30 ones; b observed on 60 rows holding 20 ones at fit time and
    `ones_seen` ones in the report frame."""
    flag = np.zeros(100)
    flag[:30] = 1.0
    b = np.random.default_rng(4).normal(size=60)

    def frame(rows: np.ndarray) -> pd.DataFrame:
        out = pd.DataFrame({"a": flag, "b": np.full(100, np.nan)})
        out.loc[rows, "b"] = b
        return out

    fit_rows = np.r_[np.arange(10, 30), np.arange(30, 70)]
    report_rows = np.r_[np.arange(30 - ones_seen, 30), np.arange(30, 90 - ones_seen)]
    return frame(fit_rows), frame(report_rows)


def test_distinguished_rows_are_counted_on_shared_rows() -> None:
    fit_frame, moved = _flag_frames(10)
    model = fit(fit_frame, numeric_bins=5)
    assert not any(s.variable in (0, 2) for s in model.suppressed)
    _withheld(report(moved, model), "a", "b", "distinguished_rows")
    fit_frame, moved = _flag_frames(11)
    _scored(report(moved, fit(fit_frame, numeric_bins=5)), "a", "b")


def _binned_frame(outside: int) -> pd.DataFrame:
    inside = np.linspace(0.0, 0.089, 100 - outside)
    far = np.linspace(0.95, 1.0, outside)
    rng = np.random.default_rng(5)
    c = rng.permutation(np.array(["p"] * 50 + ["q"] * 50, dtype=object))
    return pd.DataFrame({"x": np.r_[inside, far], "c": c})


def test_contingency_distinguished_rows_after_recoding() -> None:
    real = _binned_frame(10)
    _withheld(report(real, fit(real)), "x", "c", "distinguished_rows")
    real = _binned_frame(11)
    _scored(report(real, fit(real)), "x", "c")


def test_high_cardinality_contingency_withheld() -> None:
    rng = np.random.default_rng(6)
    labels = np.array([f"h{i:03d}" for i in range(200)] * 2, dtype=object)
    real = pd.DataFrame(
        {
            "h": rng.permutation(labels),
            "x": rng.normal(size=400),
            "c": rng.choice(np.array(["p", "q", "r"], dtype=object), 400),
        }
    )
    result = report(real, fit(real, high_cardinality_columns=["h"]))
    _withheld(result, "h", "x", "high_cardinality")
    _withheld(result, "h", "c", "high_cardinality")
    _scored(result, "x", "c")


def test_short_high_cardinality_vocabulary_next_to_a_collapsed_column() -> None:
    rng = np.random.default_rng(7)
    h = np.array([f"h{i:02d}" for i in range(25)] * 20, dtype=object)
    k = np.array([f"k{i:02d}" for i in range(25)] * 20, dtype=object)
    real = pd.DataFrame({"h": rng.permutation(h), "k": rng.permutation(k)})
    model = fit(real, high_cardinality_columns=["h"])
    assert len(model.columns[0].top_values) == 25 and model.columns[1].other_count > 0
    _withheld(report(real, model), "h", "k", "high_cardinality")


def _independent_numeric(columns: int) -> pd.DataFrame:
    rng = np.random.default_rng(8)
    return pd.DataFrame({f"v{i}": rng.normal(size=100) for i in range(columns)})


def test_release_budget_withholds_every_pair_and_keeps_the_rest() -> None:
    real = _independent_numeric(7)  # 21 latent + 21 column pairs = 42 <= 50
    result = report(real, fit(real))
    assert all(p.status == "scored" for p in result.pairs)
    real = _independent_numeric(8)  # 28 + 28 = 56 > 50
    result = report(real, fit(real))
    assert {(p.status, p.reason, p.score) for p in result.pairs} == {
        ("withheld", "release_budget", None)
    }
    assert result.pairwise_score is None
    assert result.dcr is not None and result.synth_report["schema_version"] == "synth-report/v1"


def test_undefined_pairs() -> None:
    x, y = correlated(200, 0.5, seed=9)
    real = pd.DataFrame({"x": x, "y": y, "e": [None] * 200})
    model = fit(real)
    synth = sample(model)
    _undefined = report(real, model, synth)
    assert pair(_undefined, "x", "e").reason == "empty_column"
    assert pair(_undefined, "x", "e").status == "undefined"
    constant = synth.copy()
    constant["y"] = 1.0
    p = pair(report(real, model, constant), "x", "y")
    assert (p.status, p.reason, p.score) == ("undefined", "constant_in_synthetic", None)
    sparse = synth.copy()
    sparse.loc[1:, "y"] = np.nan
    p = pair(report(real, model, sparse), "x", "y")
    assert (p.status, p.reason, p.score) == ("undefined", "too_few_synthetic_rows", None)


def test_aggregates_use_scored_pairs_only() -> None:
    rng = np.random.default_rng(10)
    x, w = correlated(300, 0.6, seed=10)
    real = pd.DataFrame({"x": x, "w": w, "c": rng.choice(np.array(["a", "b"], dtype=object), 300)})
    real.loc[rng.choice(300, 3, replace=False), "w"] = np.nan
    result = report(real, fit(real))
    scored = [p.score for p in result.pairs if p.status == "scored"]
    assert any(p.status == "withheld" for p in result.pairs)
    assert result.pairwise_score == round(sum(scored) / len(scored), 3)
