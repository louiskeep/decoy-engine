"""Unit tests for decoy_engine.quality.fidelity (V2 Phase 3 D1c).

Coverage:
  - Identity: same snapshot twice -> all per-column scores = 1.0,
    marginal = pairwise = overall = 1.0.
  - Per-kind comparators (numeric quantile RMSE, categorical TVD,
    datetime TVD, freetext length-mean diff, bool as categorical).
  - Aggregation: comparable / incomparable mixing, missing-side
    handling, equal-weight overall.
  - Joints: shared pair -> TVD on cells; pair missing in output ->
    no_output_joint sentinel.
  - Symmetry: swapping source/output preserves the score (within
    rounding) for the symmetric methods.
  - Mutation contract + determinism + JSON serializability.
"""

from __future__ import annotations

import copy
import json

import pytest

from decoy_engine.quality.fidelity import (
    QUALITY_FIDELITY_SCHEMA_VERSION,
    compute_fidelity,
)

# ── fixture builders ───────────────────────────────────────────────────────


def _numeric_col(
    *,
    quantiles: dict[str, float],
    lo: float = 0.0,
    hi: float = 100.0,
    null_count: int = 0,
    non_null_count: int = 100,
) -> dict[str, object]:
    return {
        "dtype": "float64",
        "kind": "numeric",
        "null_count": null_count,
        "non_null_count": non_null_count,
        "distinct_count": non_null_count,
        "stats": {
            "min": lo,
            "max": hi,
            "mean": (lo + hi) / 2,
            "std": (hi - lo) / 4,
            "quantiles": quantiles,
            "bin_edges": [],
            "bin_counts": [],
        },
    }


def _categorical_col(
    top_values: list[tuple[str, int]],
    other_count: int = 0,
) -> dict[str, object]:
    return {
        "dtype": "object",
        "kind": "categorical",
        "null_count": 0,
        "non_null_count": sum(c for _, c in top_values) + other_count,
        "distinct_count": len(top_values) + (1 if other_count else 0),
        "stats": {
            "top_values": [{"value": v, "count": c} for v, c in top_values],
            "other_count": other_count,
        },
    }


def _datetime_col(year_counts: list[tuple[int, int]]) -> dict[str, object]:
    return {
        "dtype": "datetime64[ns]",
        "kind": "datetime",
        "null_count": 0,
        "non_null_count": sum(c for _, c in year_counts),
        "distinct_count": len(year_counts),
        "stats": {
            "min": f"{min(y for y, _ in year_counts)}-01-01T00:00:00",
            "max": f"{max(y for y, _ in year_counts)}-12-31T00:00:00",
            "year_bins": [{"year": y, "count": c} for y, c in year_counts],
        },
    }


def _freetext_col(mean: float, max_len: int) -> dict[str, object]:
    return {
        "dtype": "object",
        "kind": "freetext",
        "null_count": 0,
        "non_null_count": 50,
        "distinct_count": 50,
        "stats": {
            "length": {"min": 1, "max": max_len, "mean": mean, "std": 5.0},
            "length_bin_edges": [],
            "length_bin_counts": [],
        },
    }


def _snap(
    columns: dict[str, dict[str, object]],
    joints: list[dict[str, object]] | None = None,
    row_count: int = 100,
) -> dict[str, object]:
    return {
        "schema_version": "distribution-snapshot/v1",
        "row_count": row_count,
        "columns": columns,
        "joints": joints or [],
    }


# ── envelope + identity ────────────────────────────────────────────────────


def test_identity_fidelity_is_perfect() -> None:
    snap = _snap(
        {
            "x": _numeric_col(
                quantiles={"p05": 5.0, "p50": 50.0, "p95": 95.0},
                lo=0.0,
                hi=100.0,
            ),
            "state": _categorical_col([("CA", 50), ("NY", 30), ("TX", 20)]),
        }
    )
    fid = compute_fidelity(snap, snap)
    assert fid["schema_version"] == QUALITY_FIDELITY_SCHEMA_VERSION
    assert fid["marginal"]["score"] == 1.0
    assert fid["pairwise"]["score"] is None  # no joints in fixture
    assert fid["overall_score"] == 1.0
    for col in fid["marginal"]["columns"]:
        assert col["comparable"] is True
        assert col["similarity"] == 1.0


def test_fidelity_is_json_serializable() -> None:
    snap = _snap({"x": _numeric_col(quantiles={"p50": 50.0})})
    encoded = json.dumps(compute_fidelity(snap, snap), sort_keys=True)
    assert isinstance(encoded, str)


def test_fidelity_does_not_mutate_inputs() -> None:
    snap = _snap({"x": _categorical_col([("a", 10)])})
    before = copy.deepcopy(snap)
    compute_fidelity(snap, snap)
    assert snap == before


def test_fidelity_is_deterministic() -> None:
    snap = _snap({"x": _categorical_col([("a", 10), ("b", 5)])})
    f1 = compute_fidelity(snap, snap)
    f2 = compute_fidelity(snap, snap)
    assert json.dumps(f1, sort_keys=True) == json.dumps(f2, sort_keys=True)


# ── per-kind comparators ────────────────────────────────────────────────────


def test_numeric_quantile_drift_lowers_score() -> None:
    src = _snap({"x": _numeric_col(quantiles={"p50": 50.0}, lo=0.0, hi=100.0)})
    out = _snap({"x": _numeric_col(quantiles={"p50": 75.0}, lo=0.0, hi=100.0)})
    fid = compute_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    # 25-point drift on 100-point range -> RMSE = 0.25 -> sim = 0.75.
    assert col["method"] == "quantile_rmse"
    assert col["similarity"] == pytest.approx(0.75)


def test_numeric_kind_mismatch_marks_incomparable() -> None:
    src = _snap({"x": _numeric_col(quantiles={"p50": 50.0})})
    out = _snap({"x": _categorical_col([("a", 10)])})
    fid = compute_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    assert col["comparable"] is False
    assert col["method"] == "kind_mismatch"
    assert fid["marginal"]["score"] is None


def test_categorical_tvd_distribution_shift() -> None:
    # Source: 50/50 split. Output: 100/0 split. TVD = 0.5; sim = 0.5.
    src = _snap({"state": _categorical_col([("CA", 50), ("NY", 50)])})
    out = _snap({"state": _categorical_col([("CA", 100)])})
    fid = compute_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    assert col["method"] == "tvd"
    assert col["similarity"] == pytest.approx(0.5)


def test_categorical_disjoint_keys_yields_zero_similarity() -> None:
    src = _snap({"state": _categorical_col([("CA", 100)])})
    out = _snap({"state": _categorical_col([("NY", 100)])})
    fid = compute_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    assert col["similarity"] == pytest.approx(0.0)


def test_datetime_year_bin_drift() -> None:
    src = _snap({"d": _datetime_col([(2022, 50), (2023, 50)])})
    # Same total mass, shifted to a new year.
    out = _snap({"d": _datetime_col([(2024, 100)])})
    fid = compute_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    assert col["method"] == "tvd"
    # All 100% of mass moved to a non-overlapping year -> TVD 1 -> sim 0.
    assert col["similarity"] == pytest.approx(0.0)


def test_freetext_length_mean_diff() -> None:
    src = _snap({"notes": _freetext_col(mean=20.0, max_len=40)})
    # Mean halved on the same max -> diff = 10 / 40 = 0.25 -> sim 0.75.
    out = _snap({"notes": _freetext_col(mean=10.0, max_len=40)})
    fid = compute_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    assert col["method"] == "length_mean_diff"
    assert col["similarity"] == pytest.approx(0.75)


def test_bool_handled_as_categorical() -> None:
    bool_col_src = _categorical_col([("True", 60), ("False", 40)])
    bool_col_src["kind"] = "bool"
    bool_col_out = _categorical_col([("True", 90), ("False", 10)])
    bool_col_out["kind"] = "bool"
    src = _snap({"active": bool_col_src})
    out = _snap({"active": bool_col_out})
    fid = compute_fidelity(src, out)
    col = fid["marginal"]["columns"][0]
    # TVD = 0.5 * (|0.6-0.9| + |0.4-0.1|) = 0.5 * 0.6 = 0.3 -> sim 0.7.
    assert col["method"] == "tvd"
    assert col["similarity"] == pytest.approx(0.7)


def test_empty_column_excluded_from_aggregate() -> None:
    empty = {
        "dtype": "object",
        "kind": "empty",
        "null_count": 100,
        "non_null_count": 0,
        "distinct_count": 0,
        "stats": {},
    }
    src = _snap(
        {
            "empty_col": empty,
            "x": _numeric_col(quantiles={"p50": 50.0}, lo=0.0, hi=100.0),
        }
    )
    fid = compute_fidelity(src, src)
    cols = {c["column"]: c for c in fid["marginal"]["columns"]}
    assert cols["empty_col"]["comparable"] is False
    assert cols["x"]["similarity"] == 1.0
    # Marginal score = mean of comparable only.
    assert fid["marginal"]["score"] == 1.0


# ── joints ─────────────────────────────────────────────────────────────────


def _joint(
    cols: list[str],
    cells: list[tuple[list[str], int]],
    other: int = 0,
) -> dict[str, object]:
    return {
        "columns": cols,
        "cell_count": len(cells),
        "cells": [{"key": k, "count": c} for k, c in cells],
        "other_count": other,
    }


def test_joint_identity_yields_perfect_pairwise() -> None:
    snap = _snap(
        {
            "a": _categorical_col([("x", 50), ("y", 50)]),
            "b": _categorical_col([("1", 60), ("2", 40)]),
        },
        joints=[
            _joint(
                ["a", "b"], [(["x", "1"], 30), (["x", "2"], 20), (["y", "1"], 30), (["y", "2"], 20)]
            ),
        ],
    )
    fid = compute_fidelity(snap, snap)
    assert fid["pairwise"]["score"] == 1.0
    assert fid["overall_score"] == 1.0


def test_joint_drift_lowers_pairwise() -> None:
    src = _snap(
        {"a": _categorical_col([("x", 100)]), "b": _categorical_col([("1", 100)])},
        joints=[_joint(["a", "b"], [(["x", "1"], 100)])],
    )
    out = _snap(
        {"a": _categorical_col([("x", 100)]), "b": _categorical_col([("1", 100)])},
        joints=[_joint(["a", "b"], [(["x", "2"], 100)])],
    )
    fid = compute_fidelity(src, out)
    # All mass moved to a non-overlapping cell -> TVD 1 -> sim 0.
    assert fid["pairwise"]["joints"][0]["similarity"] == pytest.approx(0.0)


def test_joint_missing_in_output_marked_incomparable() -> None:
    src = _snap(
        {"a": _categorical_col([("x", 50)]), "b": _categorical_col([("1", 50)])},
        joints=[_joint(["a", "b"], [(["x", "1"], 50)])],
    )
    out = _snap(
        {"a": _categorical_col([("x", 50)]), "b": _categorical_col([("1", 50)])},
        joints=[],
    )
    fid = compute_fidelity(src, out)
    j = fid["pairwise"]["joints"][0]
    assert j["comparable"] is False
    assert j["method"] == "no_output_joint"
    assert fid["pairwise"]["score"] is None


# ── aggregation ────────────────────────────────────────────────────────────


def test_overall_equal_weights_marginal_and_pairwise() -> None:
    src = _snap(
        {"a": _categorical_col([("x", 100)]), "b": _categorical_col([("1", 100)])},
        joints=[_joint(["a", "b"], [(["x", "1"], 100)])],
    )
    # Output: marginals identical (sim 1.0 both columns), joint half-shifted.
    out = _snap(
        {"a": _categorical_col([("x", 100)]), "b": _categorical_col([("1", 100)])},
        joints=[_joint(["a", "b"], [(["x", "1"], 50), (["x", "2"], 50)])],
    )
    fid = compute_fidelity(src, out)
    assert fid["marginal"]["score"] == 1.0
    # Joint cell shift: src {(x,1): 1.0}, out {(x,1): 0.5, (x,2): 0.5}
    # TVD = 0.5 * (|1.0 - 0.5| + |0 - 0.5|) = 0.5 -> sim 0.5.
    assert fid["pairwise"]["score"] == pytest.approx(0.5)
    # Equal weight: (1.0 + 0.5) / 2 = 0.75.
    assert fid["overall_score"] == pytest.approx(0.75)


def test_overall_passes_through_when_one_side_missing() -> None:
    snap = _snap({"x": _categorical_col([("a", 10)])})
    fid = compute_fidelity(snap, snap)
    # No joints in either snapshot; overall_score equals marginal.
    assert fid["pairwise"]["score"] is None
    assert fid["overall_score"] == fid["marginal"]["score"] == 1.0


# ── symmetry ───────────────────────────────────────────────────────────────


def test_symmetry_swap_does_not_change_score() -> None:
    a = _snap({"state": _categorical_col([("CA", 50), ("NY", 50)])})
    b = _snap({"state": _categorical_col([("CA", 30), ("NY", 70)])})
    f_ab = compute_fidelity(a, b)
    f_ba = compute_fidelity(b, a)
    assert f_ab["marginal"]["columns"][0]["similarity"] == pytest.approx(
        f_ba["marginal"]["columns"][0]["similarity"]
    )
