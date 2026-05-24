"""Snapshot harness for quality fidelity scoring (V2 Phase 3 D1c).

Pins SHA-256 digests for the fidelity dict across canonical fixtures.
Any change to method labels, score rounding, aggregation, or per-kind
comparator formulae fails CI with exactly one fixture name in the
message.

Fixtures cover:
  - identity (every score = 1.0)
  - numeric quantile drift (sim < 1)
  - categorical TVD shift
  - joint cell drift
  - kind mismatch (incomparable -> excluded)
  - mixed marginal + pairwise (overall is equal-weight average)

Adding / updating fixtures: same `UPDATE_SNAPSHOTS=1` pattern as
sibling harnesses.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.quality.fidelity import compute_fidelity

GOLDEN = Path(__file__).parent / "golden" / "quality_fidelity"


def _numeric_col(quantiles: dict[str, float], lo: float, hi: float) -> dict[str, Any]:
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
            "std": (hi - lo) / 4,
            "quantiles": quantiles,
            "bin_edges": [],
            "bin_counts": [],
        },
    }


def _cat_col(
    top: list[tuple[str, int]],
    other: int = 0,
) -> dict[str, Any]:
    return {
        "dtype": "object",
        "kind": "categorical",
        "null_count": 0,
        "non_null_count": sum(c for _, c in top) + other,
        "distinct_count": len(top) + (1 if other else 0),
        "stats": {
            "top_values": [{"value": v, "count": c} for v, c in top],
            "other_count": other,
        },
    }


def _snap(
    columns: dict[str, dict[str, Any]],
    joints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "distribution-snapshot/v1",
        "row_count": 100,
        "columns": columns,
        "joints": joints or [],
    }


def _joint(
    cols: list[str],
    cells: list[tuple[list[str], int]],
    other: int = 0,
) -> dict[str, Any]:
    return {
        "columns": cols,
        "cell_count": len(cells),
        "cells": [{"key": k, "count": c} for k, c in cells],
        "other_count": other,
    }


def _identity() -> tuple[dict[str, Any], dict[str, Any]]:
    snap = _snap(
        {
            "x": _numeric_col({"p05": 5.0, "p50": 50.0, "p95": 95.0}, 0.0, 100.0),
            "state": _cat_col([("CA", 50), ("NY", 50)]),
        }
    )
    return snap, snap


def _numeric_drift() -> tuple[dict[str, Any], dict[str, Any]]:
    src = _snap({"x": _numeric_col({"p05": 5.0, "p50": 50.0, "p95": 95.0}, 0.0, 100.0)})
    out = _snap({"x": _numeric_col({"p05": 10.0, "p50": 75.0, "p95": 90.0}, 0.0, 100.0)})
    return src, out


def _categorical_shift() -> tuple[dict[str, Any], dict[str, Any]]:
    src = _snap({"state": _cat_col([("CA", 50), ("NY", 50)])})
    out = _snap({"state": _cat_col([("CA", 100)])})
    return src, out


def _joint_drift() -> tuple[dict[str, Any], dict[str, Any]]:
    cols = {"a": _cat_col([("x", 100)]), "b": _cat_col([("1", 100)])}
    src = _snap(cols, joints=[_joint(["a", "b"], [(["x", "1"], 100)])])
    out = _snap(
        cols,
        joints=[_joint(["a", "b"], [(["x", "1"], 50), (["x", "2"], 50)])],
    )
    return src, out


def _kind_mismatch() -> tuple[dict[str, Any], dict[str, Any]]:
    src = _snap({"x": _numeric_col({"p50": 50.0}, 0.0, 100.0)})
    out = _snap({"x": _cat_col([("a", 100)])})
    return src, out


def _mixed_marginal_and_pairwise() -> tuple[dict[str, Any], dict[str, Any]]:
    src_cols = {"a": _cat_col([("x", 100)]), "b": _cat_col([("1", 100)])}
    src = _snap(src_cols, joints=[_joint(["a", "b"], [(["x", "1"], 100)])])
    out_cols = {"a": _cat_col([("x", 80), ("y", 20)]), "b": _cat_col([("1", 100)])}
    out = _snap(
        out_cols,
        joints=[_joint(["a", "b"], [(["x", "1"], 60), (["y", "1"], 40)])],
    )
    return src, out


FIXTURES: dict[str, Callable[[], tuple[dict[str, Any], dict[str, Any]]]] = {
    "identity": _identity,
    "numeric_drift": _numeric_drift,
    "categorical_shift": _categorical_shift,
    "joint_drift": _joint_drift,
    "kind_mismatch": _kind_mismatch,
    "mixed_marginal_and_pairwise": _mixed_marginal_and_pairwise,
}


def _digest(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _golden_path(name: str) -> Path:
    return GOLDEN / f"{name}.sha256"


@pytest.mark.parametrize("name", sorted(FIXTURES.keys()))
def test_quality_fidelity_baseline(name: str) -> None:
    src, out = FIXTURES[name]()
    fid = compute_fidelity(src, out)
    digest = _digest(fid)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        _golden_path(name).write_text(digest + "\n", encoding="utf-8")
        return

    path = _golden_path(name)
    if not path.exists():
        pytest.fail(
            f"Missing golden for fixture {name!r}. "
            f"Run with UPDATE_SNAPSHOTS=1 to create it, then inspect "
            f"{path} before committing."
        )
    expected = path.read_text(encoding="utf-8").strip()
    if expected != digest:
        pytest.fail(
            f"Quality fidelity drift on fixture {name!r}\n"
            f"  expected digest: {expected}\n"
            f"  actual digest:   {digest}\n"
            f"  actual payload:  {json.dumps(fid, indent=2, sort_keys=True)[:2000]}"
        )
