"""Unit tests for decoy_engine.quality.shape_fidelity (V2 Phase 3 D5b).

Coverage:
  - Identity case: same snapshot twice -> shape_similarity = 1.0
    on every comparable column / joint.
  - Hash-equivalent case: source {CA:50, NY:30, TX:20} compared to
    output {h1:50, h2:30, h3:20} (different keys, same shape) ->
    shape_similarity = 1.0 even though value_identity TVD would
    score it 0.0. THIS is the core D5b motivation.
  - Numeric / datetime / freetext shape comparisons.
  - Different shape -> lower similarity.
  - Vectors of different length (one snapshot has more distinct
    values) -> padding does not falsely zero the score.
  - Kind mismatch / empty / no_data handled like the D1c comparator.
  - Joint shape comparison.
  - Aggregation: comparable / incomparable mixing, equal-weight
    overall.
  - Mutation contract, determinism, JSON serializability.
"""

from __future__ import annotations

import copy
import json

import pytest

from decoy_engine.quality.shape_fidelity import (
    QUALITY_SHAPE_FIDELITY_SCHEMA_VERSION,
    compute_shape_fidelity,
)


def _cat_col(
    top: list[tuple[str, int]],
    other: int = 0,
    kind: str = "categorical",
) -> dict:
    return {
        "dtype": "object",
        "kind": kind,
        "null_count": 0,
        "non_null_count": sum(c for _, c in top) + other,
        "distinct_count": len(top) + (1 if other else 0),
        "stats": {
            "top_values": [{"value": v, "count": c} for v, c in top],
            "other_count": other,
        },
    }


def _numeric_col(bin_counts: list[int]) -> dict:
    return {
        "dtype": "float64",
        "kind": "numeric",
        "null_count": 0,
        "non_null_count": sum(bin_counts),
        "distinct_count": sum(bin_counts),
        "stats": {
            "min": 0.0,
            "max": 100.0,
            "mean": 50.0,
            "std": 25.0,
            "quantiles": {},
            "bin_edges": list(range(len(bin_counts) + 1)),
            "bin_counts": bin_counts,
        },
    }


def _snap(columns: dict, joints: list | None = None) -> dict:
    return {
        "schema_version": "distribution-snapshot/v1",
        "row_count": 100,
        "columns": columns,
        "joints": joints or [],
    }


# ── envelope ───────────────────────────────────────────────────────────────


def test_identity_shape_fidelity_is_perfect() -> None:
    snap = _snap(
        {
            "x": _numeric_col([10, 20, 30, 25, 15]),
            "state": _cat_col([("CA", 50), ("NY", 30), ("TX", 20)]),
        }
    )
    fid = compute_shape_fidelity(snap, snap)
    assert fid["schema_version"] == QUALITY_SHAPE_FIDELITY_SCHEMA_VERSION
    assert fid["marginal"]["shape_score"] == 1.0
    assert fid["overall_shape_score"] == 1.0
    for col in fid["marginal"]["columns"]:
        assert col["comparable"] is True
        assert col["shape_similarity"] == 1.0


def test_shape_fidelity_is_json_serializable() -> None:
    snap = _snap({"x": _cat_col([("a", 10)])})
    encoded = json.dumps(compute_shape_fidelity(snap, snap), sort_keys=True)
    assert isinstance(encoded, str)


def test_shape_fidelity_does_not_mutate_inputs() -> None:
    snap = _snap({"x": _cat_col([("a", 10)])})
    before = copy.deepcopy(snap)
    compute_shape_fidelity(snap, snap)
    assert snap == before


# ── THE CORE D5b MOTIVATION ────────────────────────────────────────────────


def test_hash_equivalent_value_identity_disjoint_shape_perfect() -> None:
    # Source values are {CA: 50, NY: 30, TX: 20}.
    # Output values are entirely different (simulated hash result):
    # {h1: 50, h2: 30, h3: 20}. Same frequency shape, completely
    # disjoint value sets.
    #
    # D1c value-identity TVD would score this 0.0 (TVD = 1.0).
    # D5b shape-only must score it 1.0.
    src = _snap({"state": _cat_col([("CA", 50), ("NY", 30), ("TX", 20)])})
    out = _snap({"state": _cat_col([("h1", 50), ("h2", 30), ("h3", 20)])})
    fid = compute_shape_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    assert col["shape_similarity"] == 1.0
    assert col["method"] == "freq_vector_sorted_tvd"


def test_hash_equivalent_numeric_shape_perfect() -> None:
    # Same numeric distribution shape, shifted values.
    src = _snap({"x": _numeric_col([10, 20, 30, 25, 15])})
    out = _snap({"x": _numeric_col([15, 25, 30, 20, 10])})  # same multiset, shuffled
    fid = compute_shape_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    assert col["shape_similarity"] == 1.0


# ── shape drift ────────────────────────────────────────────────────────────


def test_50_50_to_100_0_shape_drift() -> None:
    # Source 50/50 -> output 100/0 collapses to one-bucket distribution.
    # Sorted vectors: src=[0.5, 0.5], out=[1.0, 0]. TVD = 0.5 -> sim 0.5.
    src = _snap({"state": _cat_col([("CA", 50), ("NY", 50)])})
    out = _snap({"state": _cat_col([("CA", 100)])})
    fid = compute_shape_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    assert col["shape_similarity"] == pytest.approx(0.5)


def test_uneven_padding_does_not_zero_score() -> None:
    # Source has 5 distinct values; output has 3. After padding with
    # zeros, shape comparison should reflect the actual count shape
    # difference, not penalize for unequal vector lengths.
    src = _snap({"x": _cat_col([("a", 10), ("b", 10), ("c", 10), ("d", 10), ("e", 10)])})
    out = _snap({"x": _cat_col([("a", 25), ("b", 15), ("c", 10)])})
    fid = compute_shape_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    # Some drift but not 0.
    assert 0.0 < col["shape_similarity"] < 1.0


# ── kind mismatch / empty / no data ───────────────────────────────────────


def test_kind_mismatch_marks_incomparable() -> None:
    src = _snap({"x": _numeric_col([10, 20, 30])})
    out = _snap({"x": _cat_col([("a", 60)])})
    fid = compute_shape_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    assert col["comparable"] is False
    assert col["method"] == "kind_mismatch"


def test_empty_column_excluded_from_aggregate() -> None:
    empty = {
        "dtype": "object",
        "kind": "empty",
        "null_count": 100,
        "non_null_count": 0,
        "distinct_count": 0,
        "stats": {},
    }
    src = _snap({"empty_col": empty, "x": _cat_col([("a", 10), ("b", 10)])})
    fid = compute_shape_fidelity(src, src)
    cols = {c["column"]: c for c in fid["marginal"]["columns"]}
    assert cols["empty_col"]["comparable"] is False
    assert cols["x"]["shape_similarity"] == 1.0
    assert fid["marginal"]["shape_score"] == 1.0


def test_zero_count_vectors_handled() -> None:
    # Both sides have zero rows in the captured stats; method returns
    # no_data rather than dividing by zero.
    src = _snap({"x": _cat_col([], other=0)})
    fid = compute_shape_fidelity(src, src)
    col = fid["marginal"]["columns"][0]
    assert col["comparable"] is False
    assert col["method"] == "no_data"


# ── joints ─────────────────────────────────────────────────────────────────


def _joint(
    cols: list[str],
    cells: list[tuple[list[str], int]],
    other: int = 0,
) -> dict:
    return {
        "columns": cols,
        "cell_count": len(cells),
        "cells": [{"key": k, "count": c} for k, c in cells],
        "other_count": other,
    }


def test_joint_identity_yields_perfect_pairwise() -> None:
    snap = _snap(
        {"a": _cat_col([("x", 50), ("y", 50)]), "b": _cat_col([("1", 60), ("2", 40)])},
        joints=[
            _joint(
                ["a", "b"], [(["x", "1"], 30), (["x", "2"], 20), (["y", "1"], 30), (["y", "2"], 20)]
            )
        ],
    )
    fid = compute_shape_fidelity(snap, snap)
    assert fid["pairwise"]["shape_score"] == 1.0
    assert fid["overall_shape_score"] == 1.0


def test_joint_shape_preserved_with_different_keys() -> None:
    # Source pair distribution shape: {30, 20, 30, 20}.
    # Output uses different keys (hash-style) but same shape.
    src_cols = {"a": _cat_col([("x", 50), ("y", 50)]), "b": _cat_col([("1", 60), ("2", 40)])}
    out_cols = {"a": _cat_col([("h1", 50), ("h2", 50)]), "b": _cat_col([("k1", 60), ("k2", 40)])}
    src = _snap(
        src_cols,
        joints=[
            _joint(
                ["a", "b"], [(["x", "1"], 30), (["x", "2"], 20), (["y", "1"], 30), (["y", "2"], 20)]
            )
        ],
    )
    out = _snap(
        out_cols,
        joints=[
            _joint(
                ["a", "b"],
                [(["h1", "k1"], 30), (["h1", "k2"], 20), (["h2", "k1"], 30), (["h2", "k2"], 20)],
            )
        ],
    )
    fid = compute_shape_fidelity(src, out)
    joint = fid["pairwise"]["joints"][0]
    assert joint["shape_similarity"] == 1.0


def test_joint_missing_in_output_marked_incomparable() -> None:
    src_cols = {"a": _cat_col([("x", 50)]), "b": _cat_col([("1", 50)])}
    src = _snap(src_cols, joints=[_joint(["a", "b"], [(["x", "1"], 50)])])
    out = _snap(src_cols, joints=[])
    fid = compute_shape_fidelity(src, out)
    j = fid["pairwise"]["joints"][0]
    assert j["comparable"] is False
    assert j["method"] == "no_output_joint"
    assert fid["pairwise"]["shape_score"] is None


# ── aggregation + overall ──────────────────────────────────────────────────


def test_overall_equal_weights_marginal_and_pairwise() -> None:
    src_cols = {"a": _cat_col([("x", 100)]), "b": _cat_col([("1", 100)])}
    out_cols = {"a": _cat_col([("x", 100)]), "b": _cat_col([("1", 100)])}
    src = _snap(src_cols, joints=[_joint(["a", "b"], [(["x", "1"], 100)])])
    out = _snap(
        out_cols,
        joints=[_joint(["a", "b"], [(["x", "1"], 50), (["x", "2"], 50)])],
    )
    fid = compute_shape_fidelity(src, out)
    # Marginal: identity (a and b shape unchanged) -> 1.0.
    assert fid["marginal"]["shape_score"] == 1.0
    # Joint: 1-vector becomes 2-vector (1.0) vs (0.5, 0.5)
    # padded sorted: src [1.0, 0], out [0.5, 0.5] -> TVD = 0.5 -> sim 0.5.
    assert fid["pairwise"]["shape_score"] == pytest.approx(0.5)
    # Equal weight: (1.0 + 0.5) / 2 = 0.75.
    assert fid["overall_shape_score"] == pytest.approx(0.75)


def test_overall_none_when_no_comparable_anywhere() -> None:
    snap = _snap(
        {
            "x": {
                "dtype": "object",
                "kind": "empty",
                "null_count": 10,
                "non_null_count": 0,
                "distinct_count": 0,
                "stats": {},
            }
        }
    )
    fid = compute_shape_fidelity(snap, snap)
    assert fid["marginal"]["shape_score"] is None
    assert fid["overall_shape_score"] is None


# ── symmetry ──────────────────────────────────────────────────────────────


def test_symmetry_swap_preserves_shape_score() -> None:
    a = _snap({"state": _cat_col([("CA", 50), ("NY", 50)])})
    b = _snap({"state": _cat_col([("CA", 30), ("NY", 70)])})
    f_ab = compute_shape_fidelity(a, b)
    f_ba = compute_shape_fidelity(b, a)
    assert f_ab["marginal"]["columns"][0]["shape_similarity"] == pytest.approx(
        f_ba["marginal"]["columns"][0]["shape_similarity"]
    )
