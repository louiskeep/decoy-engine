"""Unit tests for decoy_engine.quality.snapshot (V2 Phase 3 D1a).

Coverage:
  - Per-kind correctness (numeric / categorical / datetime / freetext /
    empty / bool).
  - Determinism: same input -> identical bytes via canonical JSON.
  - JSON serializability without a custom encoder.
  - Input is never mutated.
  - Null handling: nulls excluded from stats, counted on the column row.
  - Joint pairs: pair normalization, missing-column skipping, top-K
    truncation with "other_count" rollup.
  - Edge cases: zero-range numeric column, all-null column, single-row
    frame.

The "expected metric pattern" assertions deliberately check the
*structure* (keys present, ordering, totals reconcile) rather than
exact bin-count values where pandas / numpy could rebin under different
versions. The byte-stable golden lives in
tests/snapshots/test_distribution_snapshot_baseline.py.
"""

from __future__ import annotations

import copy
import json

import numpy as np
import pandas as pd
import pytest

from decoy_engine.quality.snapshot import (
    DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
    compute_distribution_snapshot,
)

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mixed_frame() -> pd.DataFrame:
    rng = np.random.default_rng(seed=42)
    return pd.DataFrame(
        {
            "age": rng.integers(low=18, high=85, size=200),
            "salary": rng.normal(loc=50_000, scale=15_000, size=200),
            "state": rng.choice(["CA", "NY", "TX", "WA", "OR"], size=200),
            "joined": pd.to_datetime(
                rng.integers(low=2010, high=2025, size=200).astype(str) + "-01-01",
            ),
            "notes": [f"note-{i}-extra-words-{'x' * (i % 50)}" for i in range(200)],
            "active": rng.choice([True, False], size=200),
        }
    )


# ── schema + envelope ────────────────────────────────────────────────────────


def test_snapshot_envelope(mixed_frame: pd.DataFrame) -> None:
    snap = compute_distribution_snapshot(mixed_frame)
    assert snap["schema_version"] == DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION
    assert snap["row_count"] == 200
    assert set(snap["columns"].keys()) == set(mixed_frame.columns)
    assert snap["joints"] == []  # no joints requested


def test_snapshot_is_json_serializable(mixed_frame: pd.DataFrame) -> None:
    snap = compute_distribution_snapshot(
        mixed_frame,
        joint_columns=[("state", "active")],
    )
    # The contract is "no custom encoder needed". If a numpy scalar or a
    # Timestamp slipped through, this raises TypeError.
    encoded = json.dumps(snap, sort_keys=True)
    assert isinstance(encoded, str)
    # And the round-trip recovers the same dict.
    assert json.loads(encoded) == json.loads(encoded)


# ── determinism (the load-bearing property for fidelity diffs later) ─────────


def test_snapshot_is_deterministic(mixed_frame: pd.DataFrame) -> None:
    s1 = compute_distribution_snapshot(mixed_frame, joint_columns=[("state", "active")])
    s2 = compute_distribution_snapshot(mixed_frame, joint_columns=[("state", "active")])
    assert json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True)


def test_snapshot_does_not_mutate_input(mixed_frame: pd.DataFrame) -> None:
    before = copy.deepcopy(mixed_frame)
    compute_distribution_snapshot(mixed_frame, joint_columns=[("state", "joined")])
    pd.testing.assert_frame_equal(mixed_frame, before)


# ── per-kind correctness ─────────────────────────────────────────────────────


def test_numeric_column_has_quantiles_and_bins(mixed_frame: pd.DataFrame) -> None:
    col = compute_distribution_snapshot(mixed_frame)["columns"]["age"]
    assert col["kind"] == "numeric"
    stats = col["stats"]
    assert set(stats["quantiles"].keys()) == {"p05", "p25", "p50", "p75", "p95"}
    assert len(stats["bin_edges"]) == len(stats["bin_counts"]) + 1
    # Bin counts must reconcile to non-null count.
    assert sum(stats["bin_counts"]) == col["non_null_count"]
    assert stats["min"] <= stats["mean"] <= stats["max"]


def test_categorical_column_uses_top_k_and_other_count() -> None:
    # 30 distinct values; cap to 5 top -> 25 collapse into other.
    df = pd.DataFrame({"city": [f"c{i}" for i in range(30)] * 3})
    snap = compute_distribution_snapshot(df, categorical_top_k=5)
    col = snap["columns"]["city"]
    assert col["kind"] == "categorical"
    assert len(col["stats"]["top_values"]) == 5
    # 30 distinct values * 3 reps = 90 rows; top 5 take 15 rows, other = 75.
    top_total = sum(item["count"] for item in col["stats"]["top_values"])
    assert top_total + col["stats"]["other_count"] == col["non_null_count"]
    # HIGH-2 byte-stability: the default (non-high_cardinality) path must
    # NEVER carry the high_cardinality provenance marker.
    assert "high_cardinality" not in col["stats"]


def test_categorical_ordering_is_count_desc_then_lexical() -> None:
    df = pd.DataFrame({"x": ["b", "a", "a", "c", "b"]})  # a:2, b:2, c:1
    snap = compute_distribution_snapshot(df)
    items = snap["columns"]["x"]["stats"]["top_values"]
    assert [i["value"] for i in items] == ["a", "b", "c"]


def test_datetime_column_yields_year_bins(mixed_frame: pd.DataFrame) -> None:
    col = compute_distribution_snapshot(mixed_frame)["columns"]["joined"]
    assert col["kind"] == "datetime"
    years = [b["year"] for b in col["stats"]["year_bins"]]
    assert years == sorted(years)  # ascending
    assert sum(b["count"] for b in col["stats"]["year_bins"]) == col["non_null_count"]


def test_freetext_column_yields_length_distribution(mixed_frame: pd.DataFrame) -> None:
    col = compute_distribution_snapshot(mixed_frame)["columns"]["notes"]
    assert col["kind"] == "freetext"
    stats = col["stats"]
    assert "length" in stats
    assert stats["length"]["min"] <= stats["length"]["max"]
    assert sum(stats["length_bin_counts"]) == col["non_null_count"]


def test_bool_column_treated_as_categorical(mixed_frame: pd.DataFrame) -> None:
    col = compute_distribution_snapshot(mixed_frame)["columns"]["active"]
    assert col["kind"] == "categorical"
    values = sorted(item["value"] for item in col["stats"]["top_values"])
    assert values == ["False", "True"]


def test_empty_column_has_kind_empty() -> None:
    df = pd.DataFrame({"x": [None, None, None]}, dtype="object")
    col = compute_distribution_snapshot(df)["columns"]["x"]
    assert col["kind"] == "empty"
    assert col["null_count"] == 3
    assert col["non_null_count"] == 0
    assert col["distinct_count"] == 0
    assert col["stats"] == {}


def test_zero_range_numeric_column() -> None:
    # All values identical; np.histogram with equal min/max would zero-div.
    df = pd.DataFrame({"x": [7, 7, 7, 7]})
    col = compute_distribution_snapshot(df)["columns"]["x"]
    assert col["kind"] == "numeric"
    assert col["stats"]["min"] == col["stats"]["max"] == 7
    assert col["stats"]["bin_counts"] == [4]


def test_nulls_counted_but_excluded_from_stats() -> None:
    df = pd.DataFrame({"x": [1.0, 2.0, np.nan, 4.0, None]})
    col = compute_distribution_snapshot(df)["columns"]["x"]
    assert col["null_count"] == 2
    assert col["non_null_count"] == 3
    assert col["stats"]["mean"] == pytest.approx((1 + 2 + 4) / 3)


# ── joints ───────────────────────────────────────────────────────────────────


def test_joint_pair_normalized_to_sorted_order(mixed_frame: pd.DataFrame) -> None:
    s_ab = compute_distribution_snapshot(mixed_frame, joint_columns=[("state", "active")])
    s_ba = compute_distribution_snapshot(mixed_frame, joint_columns=[("active", "state")])
    # The pair is normalized to (active, state) since "active" < "state".
    assert s_ab["joints"] == s_ba["joints"]
    assert s_ab["joints"][0]["columns"] == ["active", "state"]


def test_joint_pair_unknown_column_silently_skipped(mixed_frame: pd.DataFrame) -> None:
    snap = compute_distribution_snapshot(
        mixed_frame,
        joint_columns=[("state", "nonexistent"), ("state", "active")],
    )
    # Only the valid pair survives.
    assert len(snap["joints"]) == 1
    assert snap["joints"][0]["columns"] == ["active", "state"]


def test_joint_self_pair_skipped(mixed_frame: pd.DataFrame) -> None:
    snap = compute_distribution_snapshot(
        mixed_frame,
        joint_columns=[("state", "state")],
    )
    assert snap["joints"] == []


def test_joint_top_k_collapse() -> None:
    # 4 distinct (a,b) cells, top_k=2 -> 2 cells in head, 2 in other_count.
    df = pd.DataFrame({"a": ["x", "x", "y", "y"] * 3, "b": ["1", "2", "1", "2"] * 3})
    snap = compute_distribution_snapshot(
        df,
        joint_columns=[("a", "b")],
        contingency_top_k=2,
    )
    joint = snap["joints"][0]
    assert len(joint["cells"]) == 2
    assert joint["cell_count"] == 4
    head = sum(c["count"] for c in joint["cells"])
    assert head + joint["other_count"] == len(df)


def test_joint_cell_ordering_is_deterministic() -> None:
    # Ties on count: secondary sort by key strings to keep ordering stable.
    df = pd.DataFrame({"a": ["x", "y", "z", "x", "y", "z"], "b": ["1", "1", "1", "2", "2", "2"]})
    snap = compute_distribution_snapshot(df, joint_columns=[("a", "b")])
    keys = [tuple(c["key"]) for c in snap["joints"][0]["cells"]]
    # All cells have count 1; sorted by (-1, a, b) which reduces to
    # ascending (a, b).
    assert keys == sorted(keys)


def test_joint_with_all_null_rows() -> None:
    df = pd.DataFrame({"a": [None, None, "x"], "b": [None, "y", None]})
    snap = compute_distribution_snapshot(df, joint_columns=[("a", "b")])
    # Only rows where both are non-null contribute. None of the rows
    # qualify here, so the joint is empty but still recorded.
    joint = snap["joints"][0]
    assert joint["cells"] == []
    assert joint["other_count"] == 0
    assert joint["cell_count"] == 0


# ── kwargs surface ──────────────────────────────────────────────────────────


def test_numeric_bins_kwarg_controls_bin_count() -> None:
    df = pd.DataFrame({"x": list(range(100))})
    snap = compute_distribution_snapshot(df, numeric_bins=4)
    bin_counts = snap["columns"]["x"]["stats"]["bin_counts"]
    assert len(bin_counts) == 4
    assert sum(bin_counts) == 100


# ── high_cardinality (HC-5) ─────────────────────────────────────────────────


def test_high_cardinality_bypasses_cliff_and_top_k() -> None:
    # 50 distinct values, one dominant: without high_cardinality this would
    # be freetext (>30 distinct); with it, categorical with full retention.
    df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(50)] + ["C000"] * 20})
    snap = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    col = snap["columns"]["code"]
    assert col["kind"] == "categorical"
    assert col["stats"]["other_count"] == 0
    assert len(col["stats"]["top_values"]) == col["distinct_count"] == 50
    top_total = sum(item["count"] for item in col["stats"]["top_values"])
    assert top_total == col["non_null_count"] == 70
    # HIGH-2: the high_cardinality path DOES carry the provenance marker.
    assert col["stats"]["high_cardinality"] is True


def test_high_cardinality_omitted_is_unaffected() -> None:
    """A column NOT in high_cardinality_columns behaves exactly as before,
    even when other columns in the same frame are marked."""
    df = pd.DataFrame(
        {
            "code": [f"C{i:03d}" for i in range(50)],
            "notes": [f"note {i} with extra words" for i in range(50)],
        }
    )
    with_flag = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    without_flag = compute_distribution_snapshot(df)
    assert with_flag["columns"]["notes"] == without_flag["columns"]["notes"]
    assert without_flag["columns"]["code"]["kind"] == "freetext"


def test_high_cardinality_preserves_deterministic_sort() -> None:
    df = pd.DataFrame({"x": ["b", "a", "a", "c", "b"] * 8})  # a:16, b:16, c:8
    snap = compute_distribution_snapshot(df, high_cardinality_columns=["x"])
    items = snap["columns"]["x"]["stats"]["top_values"]
    assert [i["value"] for i in items] == ["a", "b", "c"]


def test_high_cardinality_unknown_column_silently_skipped() -> None:
    # Matches joint_columns precedent: the collection is not a validator.
    df = pd.DataFrame({"x": ["a", "b", "c"]})
    snap = compute_distribution_snapshot(df, high_cardinality_columns=["ghost"])
    assert snap["columns"]["x"]["kind"] == "categorical"


def test_high_cardinality_numeric_dtype_rejected() -> None:
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": [1, 2, 3, 4, 5]})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_non_string_dtype"


def test_high_cardinality_datetime_dtype_rejected() -> None:
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": pd.to_datetime(["2020-01-01", "2020-01-02"])})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_non_string_dtype"


def test_high_cardinality_bool_dtype_rejected() -> None:
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": [True, False, True]})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_non_string_dtype"


def test_high_cardinality_distinct_limit_exceeded() -> None:
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": [f"C{i}" for i in range(100_001)]})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_distinct_limit_exceeded"


def test_high_cardinality_distinct_limit_boundary_ok() -> None:
    # Exactly at the limit must NOT raise.
    df = pd.DataFrame({"code": [f"C{i}" for i in range(100_000)]})
    snap = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert snap["columns"]["code"]["distinct_count"] == 100_000


def test_high_cardinality_label_bytes_limit_exceeded() -> None:
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    # 2,000 distinct labels of ~10KB each: well over the 16 MiB combined cap,
    # while staying under the 100k distinct-value cap.
    big_label = "x" * 10_000
    df = pd.DataFrame({"code": [f"{big_label}{i}" for i in range(2_000)]})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_label_bytes_limit_exceeded"


def test_high_cardinality_does_not_mutate_input() -> None:
    df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(40)]})
    before = copy.deepcopy(df)
    compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    pd.testing.assert_frame_equal(df, before)


def test_high_cardinality_is_json_serializable_and_deterministic() -> None:
    df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(40)] + ["C000"] * 5})
    s1 = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    s2 = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True)


def test_high_cardinality_mixed_object_string_coercion_collision_rejected() -> None:
    """MED-2: an object column of [1, "1", 2, "2"] has 4 raw distinct
    values but only 2 string labels after str() coercion -- two real
    categories would silently merge. Must fail loud."""
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": pd.Series([1, "1", 2, "2"], dtype=object)})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_ambiguous_string_coercion"


def test_high_cardinality_simultaneous_merge_and_split_rejected() -> None:
    """MED-2 (re-gate): a count-equality check misses a simultaneous merge +
    split. object [1, 1.0, "1"] has 2 raw distinct values (pandas treats 1 and
    1.0 as equal) and 2 string labels ("1", "1.0"), so counts match -- but the
    partition is scrambled (raw class {1, 1.0} splits across "1"/"1.0" while
    "1" also absorbs the str "1"). The bijection check must fail loud."""
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": pd.Series([1, 1.0, "1"], dtype=object)})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_ambiguous_string_coercion"


def test_high_cardinality_clean_string_column_no_collision() -> None:
    # Sanity check: a clean string column does not trip the collision gate.
    df = pd.DataFrame({"code": ["1", "2", "3", "4"]})
    snap = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert snap["columns"]["code"]["distinct_count"] == 4


def test_high_cardinality_bare_str_columns_arg_rejected() -> None:
    """MED-3: `high_cardinality_columns="code"` satisfies `Collection[str]`
    structurally and would otherwise become {"c","o","d","e"} -- the real
    "code" column silently stays freetext. Reject the bare string shape."""
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(40)]})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns="code")
    assert exc.value.code == "high_cardinality_columns_not_collection"
    # A real collection (list) still works.
    snap = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert snap["columns"]["code"]["kind"] == "categorical"


def test_high_cardinality_invalid_utf8_label_raises_typed_error() -> None:
    """Codex-LOW: a lone surrogate cannot be UTF-8 encoded; this must
    surface as the module's typed error, not a raw UnicodeEncodeError."""
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": ["a", "b", "\ud800"]})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_invalid_label_encoding"


def test_high_cardinality_timedelta_dtype_rejected() -> None:
    """Dennis-LOW-1: the dtype gate is an allow-list, not a deny-list --
    timedelta/period/interval must be rejected too, not just bool/numeric/
    datetime."""
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": pd.to_timedelta(["1 days", "2 days", "3 days"])})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_non_string_dtype"


def test_high_cardinality_period_dtype_rejected() -> None:
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": pd.period_range("2020-01", periods=3, freq="M")})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_non_string_dtype"


def test_high_cardinality_interval_dtype_rejected() -> None:
    from decoy_engine.quality.snapshot import DistributionSnapshotError

    df = pd.DataFrame({"code": pd.IntervalIndex.from_breaks([0, 1, 2, 3])})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert exc.value.code == "high_cardinality_non_string_dtype"


def test_high_cardinality_category_dtype_still_accepted() -> None:
    # Allow-list sanity check: object/string/category still pass.
    df = pd.DataFrame({"code": pd.Series(["a", "b", "c"], dtype="category")})
    snap = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    assert snap["columns"]["code"]["kind"] == "categorical"


def test_high_cardinality_all_null_column_returns_empty_without_raising() -> None:
    """Dennis-LOW-2: an all-null column early-returns kind:"empty" before
    the dtype gate, so even a numeric all-null column marked
    high_cardinality never raises -- nothing to coerce, no vocabulary to
    retain. Pin this behavior; it must not raise and must carry no marker."""
    df = pd.DataFrame({"code": pd.Series([None, None, None], dtype=object)})
    snap = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    col = snap["columns"]["code"]
    assert col["kind"] == "empty"
    assert "high_cardinality" not in col["stats"]
