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

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._categorical import (
    _WEIGHTED_CDF_RES,
    CategoricalStrategyHandler,
    _build_cdf,
)
from decoy_engine.plan._types import ColumnSeed


def _seed(
    provider_config: dict, *, deterministic: bool = False, namespace: str | None = "ns"
) -> ColumnSeed:
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
    # DE-02: keyed strategies read ctx.mask_key; no-secret path == job_seed.
    mask_key = job_seed


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

    def test_weight_below_resolution_raises(self):
        """QA-3 F9 (2026-05-31): a weight that does not move the CDF
        threshold (because it's tinier than the float-precision step
        of running / total * resolution) used to silently drop the
        category. Now raises so the operator knows their weight is
        too small. In practice this only happens at extreme values
        (~1e-20) due to floating-point precision of the float64
        running sum; the original finding overstated the threshold
        at 1e-6."""
        with pytest.raises(StrategyError, match="below_resolution|below the CDF"):
            _build_cdf([1.0, 1e-20, 1.0])

    def test_weight_just_above_resolution_passes(self):
        """A weight at ~1e-6 of the total moves the CDF by one slot
        and passes the new guard. Cited in the F9 audit but not
        actually broken: float64 precision means 1e-6 still moves the
        threshold."""
        cdf = _build_cdf([1.0, 2.0e-6, 1.0])
        assert cdf[1] > cdf[0]  # middle category has at least one slot


# ── V1 byte identity (no weights) ─────────────────────────────────


class TestV1ByteIdentity:
    def test_deterministic_uniform_unchanged(self):
        """No weights => V1 derive_index path. Verifies the
        extension didn't break byte identity."""
        df = pd.DataFrame({"col": ["a", "b", "c", "d", "e"]})
        handler = CategoricalStrategyHandler()
        out1, _ = handler.run(
            df.copy(),
            "col",
            _seed({"categories": ["X", "Y"]}, deterministic=True),
            _Ctx(),
        )
        out2, _ = handler.run(
            df.copy(),
            "col",
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
                df.copy(),
                "col",
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
            df.copy(),
            "col",
            _seed(
                {"categories": ["X", "Y", "Z"], "weights": [0.6, 0.3, 0.1]},
                deterministic=True,
            ),
            _Ctx(),
        )
        out2, _ = handler.run(
            df.copy(),
            "col",
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
            df.copy(),
            "col",
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
            df.copy(),
            "col",
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
            df.copy(),
            "col",
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
                df.copy(),
                "col",
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
            df.copy(),
            "col",
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

    def test_nulls_pass_through_in_deterministic_uniform(self):
        """Uniform (no-weights) deterministic path preserves a null in
        the MIDDLE and keeps processing the tail. A `continue -> break`
        mutation truncates the loop and desyncs the output length."""
        df = pd.DataFrame({"col": ["a", None, "c"]})
        handler = CategoricalStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"categories": ["X", "Y"]}, deterministic=True),
            _Ctx(),
        )
        assert len(out) == 3
        assert out["col"].iloc[0] in ("X", "Y")
        assert pd.isna(out["col"].iloc[1])
        assert out["col"].iloc[2] in ("X", "Y")


# ── _build_cdf raise-site machine fields ──────────────────────────
#
# str(StrategyError) embeds code, strategy AND message, so the existing
# match= assertions cannot distinguish a mutated code/strategy from the
# original when the mutation keeps the matched substring (an XX-wrap of
# the code still contains it). Pin the exact .code and .strategy so
# every code=/strategy= field mutation on each raise site is killed.


class TestCdfErrorFields:
    def test_nonpositive_error_fields(self):
        with pytest.raises(StrategyError) as exc:
            _build_cdf([0.0, 0.0, 0.0])
        assert exc.value.code == "categorical_weights_nonpositive"
        assert exc.value.strategy == "categorical"

    def test_negative_error_fields(self):
        with pytest.raises(StrategyError) as exc:
            _build_cdf([1.0, -0.5, 1.0])
        assert exc.value.code == "categorical_weights_negative"
        assert exc.value.strategy == "categorical"

    def test_below_resolution_error_fields(self):
        with pytest.raises(StrategyError) as exc:
            _build_cdf([1.0, 1e-20, 1.0])
        assert exc.value.code == "categorical_weight_below_resolution"
        assert exc.value.strategy == "categorical"

    def test_first_weight_below_resolution_raises(self):
        """A zero-width slot on the FIRST category must fail closed.
        Pins the `prev_threshold = 0` initializer: a None or 1 seed lets
        an index-0 weight that rounds to threshold 0 slip past the guard."""
        with pytest.raises(StrategyError) as exc:
            _build_cdf([1e-20, 1.0])
        assert exc.value.code == "categorical_weight_below_resolution"
        assert exc.value.strategy == "categorical"


# ── run() config-validation raise-site machine fields ─────────────


class TestRunConfigErrorFields:
    def test_not_list_categories_raises(self):
        """A plain-string `categories` (no from_profile) fails closed
        instead of iterating characters. Also pins the guard chain: any
        mutation that skips the guard lets the string through silently."""
        df = pd.DataFrame({"col": ["v1", "v2", "v3"]})
        handler = CategoricalStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "col",
                _seed({"categories": "abc"}, deterministic=True),
                _Ctx(),
            )
        assert exc.value.code == "categorical_categories_not_list"
        assert exc.value.strategy == "categorical"

    def test_from_profile_bypasses_not_list_guard(self):
        """from_profile=True signals the shape is compiler-managed, so a
        non-list `categories` must NOT trip the not-list guard."""
        df = pd.DataFrame({"col": ["v1", "v2", "v3"]})
        handler = CategoricalStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"categories": "abc", "from_profile": True}, deterministic=True),
            _Ctx(),
        )
        # No StrategyError raised; the string became a char pool.
        assert all(v in set("abc") for v in out["col"].tolist())

    def test_absent_categories_raises_requires(self):
        """No categories key at all -> fail closed. Pins the empty-list
        default on `cfg.get("categories", [])`: dropping it (or None)
        makes `list(None)` raise TypeError instead of the coded error."""
        df = pd.DataFrame({"col": ["a"]})
        handler = CategoricalStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(df.copy(), "col", _seed({}), _Ctx())
        assert exc.value.code == "categorical_requires_categories"
        assert exc.value.strategy == "categorical"

    def test_weights_shape_error_fields(self):
        df = pd.DataFrame({"col": ["a"]})
        handler = CategoricalStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "col",
                _seed(
                    {"categories": ["X", "Y", "Z"], "weights": [0.5, 0.5]},
                    deterministic=True,
                ),
                _Ctx(),
            )
        assert exc.value.code == "categorical_weights_shape"
        assert exc.value.strategy == "categorical"

    def test_requires_namespace_error_fields(self):
        df = pd.DataFrame({"col": ["a"]})
        handler = CategoricalStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "col",
                _seed({"categories": ["X", "Y"]}, deterministic=True, namespace=None),
                _Ctx(),
            )
        assert exc.value.code == "categorical_requires_namespace"
        assert exc.value.strategy == "categorical"

    def test_non_deterministic_nonpositive_error_fields(self):
        df = pd.DataFrame({"col": ["a"]})
        handler = CategoricalStrategyHandler()
        with pytest.raises(StrategyError) as exc:
            handler.run(
                df.copy(),
                "col",
                _seed(
                    {"categories": ["X", "Y"], "weights": [0.0, 0.0]},
                    deterministic=False,
                ),
                _Ctx(),
            )
        assert exc.value.code == "categorical_weights_nonpositive"
        assert exc.value.strategy == "categorical"


# ── Non-deterministic picks (unseeded rng) ────────────────────────


class TestNonDeterministicUniform:
    def test_uniform_picks_are_valid_indices(self):
        """Non-deterministic uniform path: picks must be a full-length
        array of valid category indices covering the whole pool. Pins the
        `rng.integers(0, len(categories), n)` argument set (a None/dropped
        arg or a shifted low/high yields a scalar, an out-of-range index,
        or a raise)."""
        n = 500
        df = pd.DataFrame({"col": [f"v{i}" for i in range(n)]})
        handler = CategoricalStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"categories": ["X", "Y"]}, deterministic=False),
            _Ctx(),
        )
        picks = out["col"].tolist()
        assert len(picks) == n
        assert set(picks) == {"X", "Y"}  # both indices reached, none out of range


class TestNonDeterministicWeightedNormalization:
    def test_unnormalized_weights_normalized_by_total(self):
        """Non-deterministic weighted path divides by the total so the
        probability vector sums to 1. Un-normalized weights (total != 1)
        make a `w * total` mutation produce probabilities summing to
        total**2, which numpy rejects; the correct `w / total` skews the
        picks toward the heavy category."""
        n = 2000
        df = pd.DataFrame({"col": [f"v{i}" for i in range(n)]})
        handler = CategoricalStrategyHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed(
                {"categories": ["X", "Y"], "weights": [9.0, 1.0]},
                deterministic=False,
            ),
            _Ctx(),
        )
        counts = Counter(out["col"].tolist())
        x_frac = counts.get("X", 0) / n
        assert 0.85 < x_frac < 0.95, f"X frac out of band: {x_frac:.3f}"
