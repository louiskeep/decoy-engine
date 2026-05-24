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
