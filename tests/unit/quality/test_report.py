"""Unit tests for decoy_engine.quality.report (V2 Phase 3 D1d).

Coverage:
  - End-to-end `compute_quality_report` from DataFrames.
  - Pure `assemble_quality_report` from pre-computed pieces.
  - `score_to_grade` boundaries.
  - Identity case: grade A, overall = 1.0, no warnings, no omissions.
  - Drift cases: warnings collated from diagnostic + fidelity,
    omissions collected from incomparable columns / joints.
  - job_id sets source/output kind to job_input/job_output.
  - generated_at is injectable; production path also produces a
    valid ISO timestamp.
  - Mutation contract on input DataFrames.
  - JSON serializability.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime

import pandas as pd
import pytest

from decoy_engine.quality.report import (
    QUALITY_REPORT_SCHEMA_VERSION,
    assemble_quality_report,
    compute_quality_report,
    score_to_grade,
)

FIXED_TS = "2026-05-24T00:00:00+00:00"


# ── score_to_grade ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (1.00, "A"),
        (0.96, "A"),
        (0.95, "A"),
        (0.949, "B"),
        (0.85, "B"),
        (0.849, "C"),
        (0.70, "C"),
        (0.699, "D"),
        (0.50, "D"),
        (0.499, "F"),
        (0.00, "F"),
        (None, "unavailable"),
    ],
)
def test_score_to_grade_boundaries(score: float | None, expected: str) -> None:
    assert score_to_grade(score) == expected


# ── compute_quality_report (end-to-end) ──────────────────────────────────


def test_identity_report_is_grade_a() -> None:
    df = pd.DataFrame({"x": [1, 2, 3, 4, 5], "state": ["CA", "NY", "CA", "TX", "NY"]})
    report = compute_quality_report(df, df, now_iso=FIXED_TS)
    assert report["schema_version"] == QUALITY_REPORT_SCHEMA_VERSION
    assert report["generated_at"] == FIXED_TS
    assert report["overall_score"] == 1.0
    assert report["grade"] == "A"
    assert report["warnings"] == []
    assert report["omissions"] == []
    assert report["diagnostic"]["passed"] is True


def test_compute_report_does_not_mutate_inputs() -> None:
    df = pd.DataFrame({"x": [1, 2, 3]})
    before = copy.deepcopy(df)
    compute_quality_report(df, df, now_iso=FIXED_TS)
    pd.testing.assert_frame_equal(df, before)


def test_report_is_json_serializable() -> None:
    df = pd.DataFrame({"x": [1, 2, 3], "state": ["a", "b", "c"]})
    report = compute_quality_report(df, df, now_iso=FIXED_TS)
    encoded = json.dumps(report, sort_keys=True)
    assert isinstance(encoded, str)


def test_job_id_sets_source_output_kind() -> None:
    df = pd.DataFrame({"x": [1, 2, 3]})
    report = compute_quality_report(df, df, job_id=42, now_iso=FIXED_TS)
    assert report["job_id"] == 42
    assert report["source"]["kind"] == "job_input"
    assert report["output"]["kind"] == "job_output"


def test_no_job_id_marks_kind_frame() -> None:
    df = pd.DataFrame({"x": [1, 2, 3]})
    report = compute_quality_report(df, df, now_iso=FIXED_TS)
    assert report["job_id"] is None
    assert report["source"]["kind"] == "frame"
    assert report["output"]["kind"] == "frame"


def test_fingerprints_passed_through() -> None:
    df = pd.DataFrame({"x": [1, 2, 3]})
    report = compute_quality_report(
        df,
        df,
        source_fingerprint="sha256:src",
        output_fingerprint="sha256:out",
        now_iso=FIXED_TS,
    )
    assert report["source"]["fingerprint"] == "sha256:src"
    assert report["output"]["fingerprint"] == "sha256:out"


def test_sampled_and_sample_size_recorded() -> None:
    df = pd.DataFrame({"x": [1, 2, 3]})
    report = compute_quality_report(
        df,
        df,
        sampled=True,
        sample_size=10_000,
        now_iso=FIXED_TS,
    )
    assert report["sampled"] is True
    assert report["sample_size"] == 10_000


# ── warnings + omissions collation ───────────────────────────────────────


def test_diagnostic_failure_surfaces_in_warnings() -> None:
    # Source has age as numeric; output replaces with strings -> kind drift.
    src = pd.DataFrame({"age": [25, 30, 35, 40]})
    out = pd.DataFrame({"age": ["young", "mid", "older", "old"]})
    report = compute_quality_report(src, out, now_iso=FIXED_TS)
    codes = [w["code"] for w in report["warnings"]]
    assert "kind_drift" in codes
    # Kind mismatch makes the only column incomparable -> no marginal score.
    assert report["marginal"]["score"] is None
    assert "no_comparable_columns" in codes
    assert report["overall_score"] is None
    assert report["grade"] == "unavailable"


def test_omissions_lists_incomparable_columns() -> None:
    src = pd.DataFrame({"age": [25, 30, 35, 40], "state": ["CA", "NY", "TX", "OR"]})
    out = pd.DataFrame({"age": ["a", "b", "c", "d"], "state": ["CA", "NY", "TX", "OR"]})
    report = compute_quality_report(src, out, now_iso=FIXED_TS)
    cols_omitted = [o["name"] for o in report["omissions"] if o["kind"] == "column"]
    assert cols_omitted == ["age"]
    # State should still contribute to marginal score (kind matched + identical).
    assert report["marginal"]["score"] == 1.0


def test_row_count_drift_warns() -> None:
    src = pd.DataFrame({"x": [1, 2, 3, 4, 5]})
    out = pd.DataFrame({"x": [1, 2, 3]})  # parity required by default
    report = compute_quality_report(src, out, now_iso=FIXED_TS)
    assert report["diagnostic"]["passed"] is False
    codes = [w["code"] for w in report["warnings"]]
    assert "row_count" in codes


def test_warnings_sorted_deterministically() -> None:
    # Force multiple drifts; verify the warnings list comes back
    # sorted so the same drift set always serializes identically.
    src = pd.DataFrame({"age": [25, 30, 35, 40], "state": ["CA"] * 4})
    out = pd.DataFrame({"age": ["a"] * 3, "state": ["NY"] * 3})  # row + kind drift
    r1 = compute_quality_report(src, out, now_iso=FIXED_TS)
    r2 = compute_quality_report(src, out, now_iso=FIXED_TS)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
    codes = [w["code"] for w in r1["warnings"]]
    assert codes == sorted(codes)


# ── assemble_quality_report (pure) ────────────────────────────────────────


def test_assemble_uses_pre_computed_pieces() -> None:
    src_snap = {"row_count": 100, "columns": {}, "joints": []}
    out_snap = {"row_count": 100, "columns": {}, "joints": []}
    diagnostic = {"passed": True, "checks": []}
    fidelity = {
        "marginal": {"score": 0.9, "columns": []},
        "pairwise": {"score": 0.8, "joints": []},
        "overall_score": 0.85,
    }
    report = assemble_quality_report(
        source_snapshot=src_snap,
        output_snapshot=out_snap,
        diagnostic=diagnostic,
        fidelity=fidelity,
        job_id=7,
        now_iso=FIXED_TS,
    )
    assert report["overall_score"] == 0.85
    assert report["grade"] == "B"
    assert report["source"]["row_count"] == 100
    assert report["output"]["row_count"] == 100
    assert report["job_id"] == 7


# ── generated_at production path ─────────────────────────────────────────


def test_generated_at_is_valid_iso_when_not_injected() -> None:
    df = pd.DataFrame({"x": [1, 2, 3]})
    report = compute_quality_report(df, df)
    # Round-trip: parse and re-format. Mostly a sanity check that we
    # produced a value the receiver can interpret as a timestamp.
    parsed = datetime.fromisoformat(report["generated_at"])
    assert parsed.tzinfo is not None  # tz-aware UTC string
