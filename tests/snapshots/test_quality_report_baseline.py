"""Snapshot harness for QualityReport assembly (V2 Phase 3 D1d).

Pins SHA-256 digests for the full report dict across canonical
fixtures. Any change to assembly layout, grade thresholds, warning
collation, or omission ordering fails CI with exactly one fixture
name in the message.

Fixtures cover:
  - identity (grade A, no warnings, no omissions)
  - kind drift (kind_drift warning + omission, grade = unavailable)
  - row count drift (row_count warning, marginal still scored)
  - mixed drift with sample metadata + fingerprints
  - assembled-from-pieces path (assemble_quality_report)
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from decoy_engine.quality.report import (
    assemble_quality_report,
    compute_quality_report,
)

GOLDEN = Path(__file__).parent / "golden" / "quality_report"

# Pinned timestamp so the digest is byte-stable across runs.
FIXED_TS = "2026-05-24T00:00:00+00:00"


def _identity_case() -> dict[str, Any]:
    df = pd.DataFrame(
        {
            "age": [25, 30, 35, 40, 45, 50],
            "state": ["CA", "NY", "TX", "CA", "NY", "TX"],
        }
    )
    return compute_quality_report(df, df, now_iso=FIXED_TS)


def _kind_drift_case() -> dict[str, Any]:
    src = pd.DataFrame({"age": [25, 30, 35, 40, 45, 50]})
    out = pd.DataFrame({"age": ["a", "b", "c", "d", "e", "f"]})
    return compute_quality_report(src, out, now_iso=FIXED_TS)


def _row_count_drift_case() -> dict[str, Any]:
    src = pd.DataFrame({"x": list(range(20))})
    out = pd.DataFrame({"x": list(range(10))})
    return compute_quality_report(src, out, now_iso=FIXED_TS)


def _mixed_drift_with_meta() -> dict[str, Any]:
    src = pd.DataFrame(
        {
            "age": [25, 30, 35, 40, 45, 50],
            "state": ["CA", "NY", "TX", "CA", "NY", "TX"],
        }
    )
    out = pd.DataFrame(
        {
            "age": [25, 30, 35, 40, 45, 50],
            "state": ["CA", "CA", "CA", "CA", "CA", "CA"],
        }
    )
    return compute_quality_report(
        src,
        out,
        job_id=42,
        source_fingerprint="sha256:src-fixture",
        output_fingerprint="sha256:out-fixture",
        sampled=True,
        sample_size=6,
        now_iso=FIXED_TS,
    )


def _assembled_from_pieces() -> dict[str, Any]:
    return assemble_quality_report(
        source_snapshot={
            "schema_version": "distribution-snapshot/v1",
            "row_count": 100,
            "columns": {},
            "joints": [],
        },
        output_snapshot={
            "schema_version": "distribution-snapshot/v1",
            "row_count": 100,
            "columns": {},
            "joints": [],
        },
        diagnostic={
            "schema_version": "quality-diagnostic/v1",
            "passed": True,
            "checks": [],
        },
        fidelity={
            "schema_version": "quality-fidelity/v1",
            "marginal": {"score": 0.92, "columns": []},
            "pairwise": {"score": 0.88, "joints": []},
            "overall_score": 0.9,
        },
        job_id=7,
        now_iso=FIXED_TS,
    )


FIXTURES: dict[str, Callable[[], dict[str, Any]]] = {
    "identity": _identity_case,
    "kind_drift": _kind_drift_case,
    "row_count_drift": _row_count_drift_case,
    "mixed_drift_with_meta": _mixed_drift_with_meta,
    "assembled_from_pieces": _assembled_from_pieces,
}


def _digest(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _golden_path(name: str) -> Path:
    return GOLDEN / f"{name}.sha256"


@pytest.mark.parametrize("name", sorted(FIXTURES.keys()))
def test_quality_report_baseline(name: str) -> None:
    report = FIXTURES[name]()
    digest = _digest(report)

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
            f"Quality report drift on fixture {name!r}\n"
            f"  expected digest: {expected}\n"
            f"  actual digest:   {digest}\n"
            f"  actual payload:  {json.dumps(report, indent=2, sort_keys=True)[:2000]}"
        )
