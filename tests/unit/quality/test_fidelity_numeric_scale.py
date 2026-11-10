"""FID-RANGE: numeric fidelity scale is the source IQR, with unit-free fallbacks.

Plan: docs/plans/fid-range.md. Seeds and constructions are pinned by the plan.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.quality import compute_distribution_snapshot
from decoy_engine.quality.fidelity import compute_fidelity


def _entry(fid: dict[str, Any], column: str = "x") -> dict[str, Any]:
    return next(c for c in fid["marginal"]["columns"] if c["column"] == column)


def _score(src: np.ndarray, out: np.ndarray) -> float:
    s = compute_distribution_snapshot(pd.DataFrame({"x": src}))
    o = compute_distribution_snapshot(pd.DataFrame({"x": out}))
    return float(_entry(compute_fidelity(s, o))["value_similarity"])


def _bin_uniform(src: np.ndarray, n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    counts, edges = np.histogram(src, bins=10)
    idx = rng.choice(10, size=n, p=counts / counts.sum())
    return rng.uniform(edges[idx], edges[idx + 1])


def _outlier_src() -> np.ndarray:
    return np.append(np.random.default_rng(101).normal(150.0, 20.0, 4999), 1_000_000.0)


def _price_src() -> np.ndarray:
    return np.round(np.random.default_rng(201).lognormal(3.0, 1.2, 5000), 2)


def _normal_src() -> np.ndarray:
    return np.random.default_rng(301).normal(100.0, 25.0, 2000)


def _hand(quantiles: dict[str, float], lo: float, hi: float) -> dict[str, Any]:
    return {
        "dtype": "float64",
        "kind": "numeric",
        "null_count": 0,
        "non_null_count": 100,
        "distinct_count": 100,
        "stats": {
            "min": lo,
            "max": hi,
            "mean": (lo + hi) / 2,
            "std": 1.0,
            "quantiles": quantiles,
            "bin_edges": [],
            "bin_counts": [],
        },
    }


def _hand_score(src: dict[str, Any], out: dict[str, Any]) -> float:
    def snap(col: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "distribution-snapshot/v1",
            "row_count": 100,
            "columns": {"x": col},
            "joints": [],
        }

    return float(_entry(compute_fidelity(snap(src), snap(out)))["value_similarity"])


def test_single_outlier_wrong_shape_scores_low() -> None:
    src = _outlier_src()
    assert _score(src, _bin_uniform(src, 5000, 102)) <= 0.10


def test_single_outlier_good_fit_stays_high() -> None:
    src = _outlier_src()
    out = np.append(np.random.default_rng(103).normal(150.0, 20.0, 4999), 1_000_000.0)
    assert _score(src, out) >= 0.95


def test_skewed_price_wrong_shape_scores_low() -> None:
    src = _price_src()
    assert _score(src, _bin_uniform(src, 5000, 202)) <= 0.10


def test_skewed_price_good_fit_stays_high() -> None:
    src = _price_src()
    out = np.round(np.random.default_rng(203).lognormal(3.0, 1.2, 5000), 2)
    assert _score(src, out) >= 0.85


def test_normal_good_fit_stays_high() -> None:
    src = _normal_src()
    assert _score(src, np.random.default_rng(302).normal(100.0, 25.0, 2000)) >= 0.90
    assert _score(src, _bin_uniform(src, 2000, 303)) >= 0.90


def test_half_sd_shift_is_penalized() -> None:
    src = _normal_src()
    out = np.random.default_rng(304).normal(112.5, 25.0, 2000)
    assert 0.40 <= _score(src, out) <= 0.75


def test_iqr_score_is_unit_free() -> None:
    src = _normal_src()
    out = np.random.default_rng(304).normal(112.5, 25.0, 2000)
    base = _score(src, out)
    assert _score(src * 1000, out * 1000) == pytest.approx(base, abs=1e-6)
    assert _score(src * 0.001, out * 0.001) == pytest.approx(base, abs=1e-6)


def test_small_unit_column_drift_is_seen() -> None:
    src = np.random.default_rng(401).normal(0.005, 0.001, 5000)
    assert _score(src, src + 0.003) <= 0.10


def test_zero_iqr_falls_back_to_p05_p95() -> None:
    q = {"p05": 0.0, "p25": 10.0, "p50": 10.0, "p75": 10.0, "p95": 20.0}
    src = _hand(q, -100.0, 100.0)
    out = _hand({**q, "p50": 12.0}, -100.0, 100.0)
    assert _hand_score(src, out) == pytest.approx(0.955279, abs=1e-6)


def test_zero_spread_fallback_is_unit_free() -> None:
    src = np.concatenate([np.zeros(97), np.full(3, 100.0)])
    out = np.concatenate([np.zeros(90), np.full(10, 100.0)])
    for k in (1.0, 1000.0, 0.001):
        assert _score(src * k, out * k) == pytest.approx(0.552786, abs=1e-6)


def test_constant_column() -> None:
    assert _score(np.full(5000, 7.0), np.full(5000, 7.0)) == 1.0
    assert _score(np.full(5000, 7.0), np.full(5000, 8.0)) == 0.0
    assert _score(np.full(5000, 0.007), np.full(5000, 0.008)) == 0.0


def test_partial_grid_uses_range() -> None:
    src = _hand({"p50": 50.0}, 0.0, 100.0)
    out = _hand({"p50": 75.0}, 0.0, 100.0)
    assert _hand_score(src, out) == pytest.approx(0.75)


def test_numeric_entry_shape_unchanged() -> None:
    src = _normal_src()
    s = compute_distribution_snapshot(pd.DataFrame({"x": src}))
    entry = _entry(compute_fidelity(s, s))
    assert entry["method"] == "quantile_rmse"
    assert set(entry) == {
        "column",
        "similarity",
        "value_similarity",
        "missing_value_similarity",
        "method",
        "comparable",
        "extra_metrics",
    }
