"""Edge-case coverage for walk_dataframe (N1 from slice-2 review).

Dennis verified these manually in the slice-2 review; this file pins
them in CI so any future refactor that breaks the behavior is caught.
Covers: duplicate-column-name rejection (L1), mixed-object dtype,
tz-aware datetime, nullable Boolean, distinct-from-sample assertion,
full-object cross-seed equality, zero-column DataFrame.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pandas as pd
import pytest

from decoy_engine.profile._walk import walk_dataframe


def _rng(seed: int = 42) -> random.Random:
    return random.Random(seed)


class TestDuplicateColumnGuard:
    """L1 from slice-2 review: hand-constructed duplicate-column DataFrames
    raise a clean ValueError instead of a downstream TypeError."""

    def test_duplicate_column_names_raise_value_error(self) -> None:
        df = pd.DataFrame([[1, 2], [3, 4]], columns=["a", "a"])
        with pytest.raises(ValueError, match="duplicate column names"):
            walk_dataframe(
                df,
                table_name="t",
                declared_pk_cols=frozenset(),
                fk_specs={},
                sample_rows=None,
                rng=_rng(),
            )

    def test_error_message_lists_duplicates(self) -> None:
        df = pd.DataFrame([[1, 2, 3, 4]], columns=["a", "b", "a", "b"])
        with pytest.raises(ValueError, match=r"\['a', 'b'\]"):
            walk_dataframe(
                df,
                table_name="t",
                declared_pk_cols=frozenset(),
                fk_specs={},
                sample_rows=None,
                rng=_rng(),
            )


class TestMixedObjectDtype:
    """Columns of dtype=object mixing strings, ints, and None."""

    def test_mixed_object_walks_cleanly(self) -> None:
        df = pd.DataFrame({"mixed": ["a", 1, None, "b", 2.5, "c"]})
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        col = profile.columns[0]
        assert col.dtype == "object"
        assert col.null_count == 1
        assert col.distinct_count == 5  # "a", 1, "b", 2.5, "c"


class TestTimezoneAwareDatetime:
    """tz-aware datetime columns report a usable dtype string."""

    def test_tz_aware_datetime_walks_cleanly(self) -> None:
        df = pd.DataFrame(
            {
                "ts": pd.to_datetime(
                    [
                        datetime(2026, 1, 1, tzinfo=timezone.utc),
                        datetime(2026, 1, 2, tzinfo=timezone.utc),
                        datetime(2026, 1, 1, tzinfo=timezone.utc),
                    ]
                )
            }
        )
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        col = profile.columns[0]
        assert "datetime" in col.dtype.lower()
        assert "utc" in col.dtype.lower()
        assert col.null_count == 0
        assert col.distinct_count == 2


class TestNullableBoolean:
    """pandas nullable BooleanDtype with NA values."""

    def test_nullable_boolean_walks_cleanly(self) -> None:
        df = pd.DataFrame({"flag": pd.array([True, False, None, True], dtype="boolean")})
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        col = profile.columns[0]
        assert col.dtype == "boolean"
        assert col.null_count == 1
        assert col.distinct_count == 2


class TestDistinctFromSample:
    """When sample is small enough to miss values, distinct_count drops below
    the true full-scan distinct_count. Verifies the sample-vs-full divergence
    is observable (the planner should not trust a sampled cardinality)."""

    def test_small_sample_undercounts_distinct(self) -> None:
        df = pd.DataFrame({"id": list(range(1000))})
        full_scan = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        sampled = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=50,
            rng=_rng(),
        )
        assert full_scan.columns[0].distinct_count == 1000
        assert sampled.columns[0].distinct_count is not None
        assert sampled.columns[0].distinct_count <= 50
        assert sampled.columns[0].distinct_count < full_scan.columns[0].distinct_count


class TestCrossSeedEquality:
    """Same seed + same DataFrame + same metadata produces equal TableProfile
    object (not just equal hashes - full object equality)."""

    def test_same_seed_produces_equal_table_profiles(self) -> None:
        df = pd.DataFrame({"a": list(range(500)), "b": list(range(500, 1000))})
        p1 = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset({"a"}),
            fk_specs={"b": ("other", "id")},
            sample_rows=100,
            rng=_rng(seed=777),
        )
        p2 = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset({"a"}),
            fk_specs={"b": ("other", "id")},
            sample_rows=100,
            rng=_rng(seed=777),
        )
        assert p1 == p2


class TestZeroColumnDataFrame:
    """A DataFrame with no columns produces a TableProfile with an empty
    columns tuple. Row count is preserved."""

    def test_zero_column_dataframe(self) -> None:
        df = pd.DataFrame(index=range(5))
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        assert profile.row_count == 5
        assert profile.columns == ()

    def test_zero_column_zero_row_dataframe(self) -> None:
        df = pd.DataFrame()
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        assert profile.row_count == 0
        assert profile.columns == ()
