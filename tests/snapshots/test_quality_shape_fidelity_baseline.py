"""Snapshot harness for shape-only fidelity (V2 Phase 3 D5b).

Pins SHA-256 digests for the shape_fidelity dict across canonical
fixtures.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.quality.shape_fidelity import compute_shape_fidelity

GOLDEN = Path(__file__).parent / "golden" / "quality_shape_fidelity"


def _cat_col(top: list[tuple[str, int]], other: int = 0) -> dict:
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


def _identity() -> dict[str, Any]:
    snap = _snap(
        {
            "x": _numeric_col([10, 20, 30, 25, 15]),
            "state": _cat_col([("CA", 50), ("NY", 30), ("TX", 20)]),
        }
    )
    return compute_shape_fidelity(snap, snap)


def _hash_equivalent_shape_perfect() -> dict[str, Any]:
    src = _snap({"state": _cat_col([("CA", 50), ("NY", 30), ("TX", 20)])})
    out = _snap({"state": _cat_col([("h1", 50), ("h2", 30), ("h3", 20)])})
    return compute_shape_fidelity(src, out)


def _shape_drift() -> dict[str, Any]:
    src = _snap({"state": _cat_col([("CA", 50), ("NY", 50)])})
    out = _snap({"state": _cat_col([("CA", 100)])})
    return compute_shape_fidelity(src, out)


def _numeric_same_multiset() -> dict[str, Any]:
    src = _snap({"x": _numeric_col([10, 20, 30, 25, 15])})
    out = _snap({"x": _numeric_col([15, 25, 30, 20, 10])})
    return compute_shape_fidelity(src, out)


FIXTURES: dict[str, Callable[[], dict[str, Any]]] = {
    "identity": _identity,
    "hash_equivalent_shape_perfect": _hash_equivalent_shape_perfect,
    "shape_drift": _shape_drift,
    "numeric_same_multiset": _numeric_same_multiset,
}


def _digest(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _golden_path(name: str) -> Path:
    return GOLDEN / f"{name}.sha256"


@pytest.mark.parametrize("name", sorted(FIXTURES.keys()))
def test_quality_shape_fidelity_baseline(name: str) -> None:
    fid = FIXTURES[name]()
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
            f"Quality shape fidelity drift on fixture {name!r}\n"
            f"  expected digest: {expected}\n"
            f"  actual digest:   {digest}\n"
            f"  actual payload:  {json.dumps(fid, indent=2, sort_keys=True)[:2000]}"
        )
