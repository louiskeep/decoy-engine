"""Snapshot harness for quality diagnostic (V2 Phase 3 D1b).

Mirror of test_distribution_snapshot_baseline.py: pins SHA-256 digests
for the diagnostic dict across canonical (source, output, kwargs)
fixtures. Any change to check ordering, key naming, threshold
defaults, or the "passed" semantics fails CI with exactly one fixture
name in the message.

Fixtures cover:
  - identity (every check passes)
  - missing column (column_survival fails)
  - row count drift, parity required (row_count fails)
  - row count drift, parity opted out (row_count passes)
  - kind drift (kind_drift fails)
  - null drift past threshold (null_drift fails)
  - all four checks failing simultaneously

Adding / updating fixtures: same `UPDATE_SNAPSHOTS=1` pattern as the
sibling harnesses; commit message must explain WHY any baseline
change was intentional.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.quality.diagnostic import compute_diagnostic

GOLDEN = Path(__file__).parent / "golden" / "quality_diagnostic"


def _col(
    *,
    kind: str,
    null_count: int = 0,
    non_null_count: int = 100,
    distinct_count: int = 5,
    dtype: str = "object",
) -> dict[str, Any]:
    return {
        "dtype": dtype,
        "kind": kind,
        "null_count": null_count,
        "non_null_count": non_null_count,
        "distinct_count": distinct_count,
        "stats": {},
    }


def _snap(
    columns: dict[str, dict[str, Any]],
    row_count: int = 100,
) -> dict[str, Any]:
    return {
        "schema_version": "distribution-snapshot/v1",
        "row_count": row_count,
        "columns": columns,
        "joints": [],
    }


def _base_source() -> dict[str, Any]:
    return _snap(
        {
            "name": _col(kind="freetext"),
            "age": _col(kind="numeric", null_count=5, non_null_count=95, dtype="int64"),
            "state": _col(kind="categorical"),
        }
    )


def _identity_case() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    src = _base_source()
    return src, src, {}


def _missing_column_case() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    src = _base_source()
    out_cols = {k: v for k, v in src["columns"].items() if k != "age"}
    return src, _snap(out_cols), {}


def _row_count_drift_parity_required() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    src = _base_source()
    return src, _snap(src["columns"], row_count=80), {}


def _row_count_drift_parity_opt_out() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    src = _base_source()
    return src, _snap(src["columns"], row_count=80), {"expect_row_parity": False}


def _kind_drift_case() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    src = _base_source()
    out_cols = json.loads(json.dumps(src["columns"]))
    out_cols["age"]["kind"] = "freetext"
    return src, _snap(out_cols), {}


def _null_drift_case() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    src = _base_source()
    out_cols = json.loads(json.dumps(src["columns"]))
    out_cols["age"]["null_count"] = 50
    out_cols["age"]["non_null_count"] = 50
    return src, _snap(out_cols), {}


def _all_checks_fail() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    src = _base_source()
    out_cols = json.loads(json.dumps(src["columns"]))
    del out_cols["name"]  # survival
    out_cols["age"]["kind"] = "freetext"  # kind
    out_cols["age"]["null_count"] = 50  # null
    out_cols["age"]["non_null_count"] = 50
    return src, _snap(out_cols, row_count=42), {}  # row_count


FIXTURES: dict[
    str,
    Callable[[], tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
] = {
    "identity": _identity_case,
    "missing_column": _missing_column_case,
    "row_count_drift_parity_required": _row_count_drift_parity_required,
    "row_count_drift_parity_opt_out": _row_count_drift_parity_opt_out,
    "kind_drift": _kind_drift_case,
    "null_drift": _null_drift_case,
    "all_checks_fail": _all_checks_fail,
}


def _digest(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _golden_path(name: str) -> Path:
    return GOLDEN / f"{name}.sha256"


@pytest.mark.parametrize("name", sorted(FIXTURES.keys()))
def test_quality_diagnostic_baseline(name: str) -> None:
    src, out, kwargs = FIXTURES[name]()
    diag = compute_diagnostic(src, out, **kwargs)  # type: ignore[arg-type]
    digest = _digest(diag)

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
            f"Quality diagnostic drift on fixture {name!r}\n"
            f"  expected digest: {expected}\n"
            f"  actual digest:   {digest}\n"
            f"  actual payload:  {json.dumps(diag, indent=2, sort_keys=True)[:2000]}"
        )
