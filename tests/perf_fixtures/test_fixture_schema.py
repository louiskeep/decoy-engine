"""PERF.BASE.2: schema-consistency tests for the fixture suite.

The fixture suite exists so per-strategy benchmarks can compare apples
to apples across scales. That contract only holds if every tier has
the same strategy-tagged columns with the same dtypes. These tests
catch silent drift (someone renames a column, an int turns into a
float, etc.) before it shows up as a confusing benchmark delta.
"""

from __future__ import annotations

import pandas as pd
import pytest

from .loaders import available_tiers, load_tier
from .schema import COMMON_COLUMNS, MEDIUM_EXTRA_COLUMNS, get_tier

pytestmark = pytest.mark.perf


def _available(tier_names: list[str]) -> list[str]:
    """Filter to the tiers whose Parquet currently exists."""
    have = set(available_tiers())
    return [t for t in tier_names if t in have]


class TestPerTierSchema:
    """Each tier exists, loads, has the row + column count its TierSpec promised."""

    @pytest.mark.parametrize("tier_name", _available(["small", "medium", "large"]))
    def test_tier_row_count_matches_spec(self, tier_name: str) -> None:
        df = load_tier(tier_name)
        tier = get_tier(tier_name)
        assert len(df) == tier.rows, (
            f"tier {tier_name!r}: expected {tier.rows} rows, got {len(df)}"
        )

    @pytest.mark.parametrize("tier_name", _available(["small", "medium", "large"]))
    def test_tier_column_set_matches_spec(self, tier_name: str) -> None:
        df = load_tier(tier_name)
        tier = get_tier(tier_name)
        spec_names = [c.name for c in tier.columns]
        assert list(df.columns) == spec_names, (
            f"tier {tier_name!r}: column order/set drift between spec + fixture"
        )


class TestCrossTierConsistency:
    """The strategy-tagged columns must look the same in every tier.

    "Look the same" means same column name + same pandas dtype. Without
    this, a per-strategy timing comparison across tiers is meaningless
    (a dtype shift would change what's actually being benchmarked).
    """

    def test_common_columns_present_in_every_tier(self) -> None:
        available = _available(["small", "medium", "large"])
        if not available:
            pytest.skip("no fixtures available on disk")
        common_names = {c.name for c in COMMON_COLUMNS}
        for tier_name in available:
            df = load_tier(tier_name)
            missing = common_names - set(df.columns)
            assert not missing, (
                f"tier {tier_name!r} missing common columns: {sorted(missing)}"
            )

    def test_common_columns_have_same_dtype_across_tiers(self) -> None:
        # Need at least two tiers on disk for the comparison to mean
        # anything; one tier is trivially consistent with itself.
        available = _available(["small", "medium", "large"])
        if len(available) < 2:
            pytest.skip("need >= 2 tiers on disk for cross-tier dtype check")
        dtypes_by_tier: dict[str, pd.Series] = {
            t: load_tier(t).dtypes for t in available
        }
        base_tier = available[0]
        for other in available[1:]:
            for col_spec in COMMON_COLUMNS:
                base_dtype = dtypes_by_tier[base_tier][col_spec.name]
                other_dtype = dtypes_by_tier[other][col_spec.name]
                assert base_dtype == other_dtype, (
                    f"dtype drift on {col_spec.name!r}: "
                    f"{base_tier}={base_dtype} vs {other}={other_dtype}"
                )


class TestMediumExtras:
    """Medium-tier extras (notes + filler) appear in medium AND large."""

    def test_medium_extras_present_in_medium(self) -> None:
        if "medium" not in _available(["medium"]):
            pytest.skip("medium tier not on disk")
        df = load_tier("medium")
        for col in MEDIUM_EXTRA_COLUMNS:
            assert col.name in df.columns, f"medium missing extra column {col.name!r}"

    def test_medium_extras_present_in_large(self) -> None:
        if "large" not in _available(["large"]):
            pytest.skip("large tier not on disk (regenerate to enable this check)")
        df = load_tier("large")
        for col in MEDIUM_EXTRA_COLUMNS:
            assert col.name in df.columns, f"large missing extra column {col.name!r}"
