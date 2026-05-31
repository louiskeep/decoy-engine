"""MG-1 S5 (2026-06-01): categorical `weights` extension regression cells.

The V1 byte-identical uniform path is preserved when weights is None.
The new shape unlocks distribution-faithful generation: a column that
should be 60/30/10 across [free, pro, team] picks at those rates
instead of uniformly.

The from_profile=True path is exercised by the plan-compile
integration test once that wiring lands; this module covers the
runtime contract (weights -> CDF -> picks).
"""

from __future__ import annotations

from collections import Counter

import pandas as pd
import pytest

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._categorical import (
    CategoricalStrategyHandler,
    _build_cdf,
    _WEIGHTED_CDF_RES,
)
from decoy_engine.plan._types import ColumnSeed


def _seed(provider_config: dict, *, deterministic: bool = False, namespace: str | None = "ns") -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="categorical",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=deterministic,
        provider_config=tuple(sorted(provider_config.items())),
    )


class _Ctx:
    # job_seed length must match decoy_engine.determinism's
    # SEED_LENGTH (8 bytes per the S3 spec).
    job_seed = (0x0123456789).to_bytes(8, "big")


# ── CDF builder ───────────────────────────────────────────────────


class TestCdfBuilder:
    def test_equal_weights_split_evenly(self):
        cdf = _build_cdf([1.0, 1.0, 1.0])
        # 3 buckets, last is the full resolution.
        assert cdf[0] < cdf[1] < cdf[2]
        assert cdf[2] == _WEIGHTED_CDF_RES
        # Each band is roughly equal.
        assert abs((cdf[1] - cdf[0]) - (cdf[0])) < 5
        assert abs((cdf[2] - cdf[1]) - (cdf[0])) < 5

    def test_skewed_weights_skew_cdf(self):
        cdf = _build_cdf([0.6, 0.3, 0.1])
        # 60% / 30% / 10% bands.
        assert cdf[0] == 600_000
        assert cdf[1] == 900_000
        assert cdf[2] == _WEIGHTED_CDF_RES

    def test_zero_weight_collapses_band(self):
        cdf = _build_cdf([1.0, 0.0, 1.0])
        # Middle category gets zero width.
        assert cdf[0] == cdf[1]
        assert cdf[2] == _WEIGHTED_CDF_RES

    def test_negative_weight_raises(self):
        with pytest.raises(StrategyError, match="negative"):
            _build_cdf([1.0, -0.5, 1.0])

    def test_all_zero_weights_raises(self):
        with pytest.raises(StrategyError, match="nonpositive"):
            _build_cdf([0.0, 0.0, 0.0])


# ── V1 byte identity (no weights) ─────────────────────────────────


class TestV1ByteIdentity:
    def test_deterministic_uniform_unchanged(self):
        """No weights => V1 derive_index path. Verifies the
        extension didn't break byte identity."""
        df = pd.DataFrame({"col": ["a", "b", "c", "d", "e"]})
        handler = CategoricalStrategyHandler()
        out1, _ = handler.run(
            df.copy(), "col",
            _seed({"categories": ["X", "Y"]}, deterministic=True),
            _Ctx(),
        )
        out2, _ = handler.run(
            df.copy(), "col",
            _seed({"categories": ["X", "Y"]}, deterministic=True),
            _Ctx(),
        )
        assert out1["col"].tolist() == out2["col"].tolist()
        # Every value is one of the categories.
        for v in out1["col"].tolist():
            assert v in ("X", "Y")


# ── Weighted deterministic path ───────────────────────────────────


class TestWeightedDeterministic:
    def test_weights_must_match_length(self):
        df = pd.DataFrame({"col": ["a"]})
        handler = CategoricalStrategyHandler()
        with pytest.raises(StrategyError, match="weights_shape"):
            handler.run(
                df.copy(), "col",
                _seed(
                    {"categories": ["X", "Y", "Z"], "weights": [0.5, 0.5]},
                    deterministic=True,
                ),
                _Ctx(),
            )

    def test_deterministic_weighted_round_trip(self):
        """Same value + same weights => same category."""
        df = pd.DataFrame({"col": ["alice"]})
        handler = CategoricalStrategyHandler()
        out1, _ = handler.run(
            df.copy(), "col",
            _seed(
                {"categories": ["X", "Y", "Z"], "weights": [0.6, 0.3, 0.1]},
                deterministic=True,
            ),
            _Ctx(),
        )
        out2, _ = handler.run(
            df.copy(), "col",
            _seed(
                {"categories": ["X", "Y", "Z"], "weights": [0.6, 0.3, 0.1]},
                deterministic=True,
            ),
            _Ctx(),
        )
        assert out1["col"].tolist() == out2["col"].tolist()

    def test_weighted_distribution_skews_toward_higher_weight(self):
        """Across a large sample of distinct source values, the
        weighted picks skew toward the high-weight category."""
        # 1000 unique source values so each gets independent picks.
        sources = [f"v{i}" for i in range(2000)]
        df = pd.DataFrame({"col": sources})
        handler = CategoricalStrategyHandler()
        out, _ = handler.run(
            df.copy(), "col",
            _seed(
                {"categories": ["X", "Y", "Z"], "weights": [0.6, 0.3, 0.1]},
                deterministic=True,
            ),
            _Ctx(),
        )
        counts = Counter(out["col"].tolist())
        total = sum(counts.values())
        # Allow generous tolerance because derive_index is uniform
        # over 1M buckets and rounding adds noise at 2000 samples.
        x_frac = counts.get("X", 0) / total
        y_frac = counts.get("Y", 0) / total
        z_frac = counts.get("Z", 0) / total
        assert 0.5 < x_frac < 0.7, f"X frac out of band: {x_frac:.3f}"
        assert 0.2 < y_frac < 0.4, f"Y frac out of band: {y_frac:.3f}"
        assert 0.05 < z_frac < 0.18, f"Z frac out of band: {z_frac:.3f}"

    def test_zero_weight_category_never_picked(self):
        """Category with weight=0 must not appear in deterministic
        output regardless of input."""
        sources = [f"v{i}" for i in range(500)]
        df = pd.DataFrame({"col": sources})
        handler = CategoricalStrategyHandler()
        out, _ = handler.run(
            df.copy(), "col",
            _seed(
                {"categories": ["X", "Y", "Z"], "weights": [1.0, 0.0, 1.0]},
                deterministic=True,
            ),
            _Ctx(),
        )
        assert "Y" not in out["col"].tolist()


# ── Weighted non-deterministic path ───────────────────────────────


class TestWeightedRandom:
    def test_non_deterministic_weighted_picks_skew(self):
        sources = [f"v{i}" for i in range(2000)]
        df = pd.DataFrame({"col": sources})
        handler = CategoricalStrategyHandler()
        out, _ = handler.run(
            df.copy(), "col",
            _seed(
                {"categories": ["X", "Y"], "weights": [0.9, 0.1]},
                deterministic=False,
            ),
            _Ctx(),
        )
        counts = Counter(out["col"].tolist())
        total = sum(counts.values())
        x_frac = counts.get("X", 0) / total
        assert 0.85 < x_frac < 0.95, f"X frac out of band: {x_frac:.3f}"

    def test_non_deterministic_weights_nonpositive_raises(self):
        df = pd.DataFrame({"col": ["a"]})
        handler = CategoricalStrategyHandler()
        with pytest.raises(StrategyError, match="nonpositive"):
            handler.run(
                df.copy(), "col",
                _seed(
                    {"categories": ["X", "Y"], "weights": [0.0, 0.0]},
                    deterministic=False,
                ),
                _Ctx(),
            )


# ── Nulls ─────────────────────────────────────────────────────────


class TestNullPreservation:
    def test_nulls_pass_through_in_deterministic_weighted(self):
        df = pd.DataFrame({"col": ["a", None, "c"]})
        handler = CategoricalStrategyHandler()
        out, _ = handler.run(
            df.copy(), "col",
            _seed(
                {"categories": ["X", "Y"], "weights": [0.5, 0.5]},
                deterministic=True,
            ),
            _Ctx(),
        )
        # Index 1 stays null; others land in the category set.
        assert out["col"].iloc[0] in ("X", "Y")
        assert pd.isna(out["col"].iloc[1])
        assert out["col"].iloc[2] in ("X", "Y")
