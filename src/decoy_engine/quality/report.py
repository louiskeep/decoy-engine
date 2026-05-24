"""QualityReport assembly: snapshot + diagnostic + fidelity -> v1 JSON.

V2 Phase 3 Distribution Integrity, Sprint D1d.

This is the top of the D1 stack. It exposes:

  - `compute_quality_report(source_df, output_df, ...)` -- end-to-end
    from two DataFrames to a QualityReport dict. The path most
    callers take.
  - `assemble_quality_report(source_snapshot, output_snapshot,
    diagnostic, fidelity, ...)` -- pure assembly from pre-computed
    pieces, used when the caller needs to snapshot once and reuse
    (e.g. D2 job integration will snapshot both sides at the runner
    and then pass dicts to multiple consumers without re-walking).
  - `score_to_grade(score)` -- maps overall_score in [0, 1] to a
    letter grade (A / B / C / D / F / "unavailable").

Schema is `quality-report/v1`, per the implementation guide
`docs/backlog/v2/plans/decoy-platform/2026-05-18-distribution-integrity-
current-state-and-implementation-guide-v2.md` "QualityReport Minimum
Shape" section. The fields present in this module match that shape
exactly so D2 can persist + serve the dict without a translation
layer.

What the report contains:

  - schema_version, generated_at, job_id (optional)
  - source / output metadata: {kind, fingerprint}
  - diagnostic: the D1b structural check block, nested as-is
  - marginal: {score, columns} from D1c
  - pairwise: {score, joints} from D1c (renamed to `pairs` in the
    guide's example; we keep `joints` to match the snapshot
    vocabulary, with a `pairs` alias for backward-compat readers --
    revisit if D2 wants to align on one name)
  - overall_score: equal-weight marginal + pairwise (passes through
    from D1c)
  - grade: A/B/C/D/F derived from overall_score
  - sampled / sample_size: caller-provided; for D2 to fill when
    runner samples large frames before snapshotting
  - warnings: collated from diagnostic + fidelity (kind drift,
    no-comparable-data, etc.)
  - omissions: per-column / per-joint exclusions from the score
    (kind mismatches, empty columns, missing joint pairs)

Determinism:
  - The `generated_at` field is injected via the `now_iso` parameter
    so tests and snapshot harnesses can pin it. Production callers
    pass `now_iso=None`, which uses `datetime.now(UTC).isoformat()`.
  - All score fields are rounded by their source modules (D1c
    rounds to 6 places); we do not re-round here.
  - Warnings / omissions lists are sorted deterministically by their
    natural keys.

Grade thresholds (industry-standard quality letter grades):
  A: >= 0.95   (near-perfect preservation)
  B: >= 0.85   (good preservation, minor drift)
  C: >= 0.70   (acceptable for most analytic workloads)
  D: >= 0.50   (significant drift; investigate before relying on)
  F:  < 0.50   (preservation broken; do not rely on)
  unavailable: score is None (no comparable columns / joints)

The thresholds are deliberately conservative on the low end so a
single bad strategy doesn't drag a mostly-good report below C.
D4 (strategy-aware policy) may revisit per-strategy grade
boundaries, but the default mapping needs to be honest: 0.5
overall similarity is genuinely poor for downstream analytics.

Out of scope for D1d (D2/D3/D4 own these):
  - Persistence to a job record (D2).
  - Surface in the Reporting UI (D3).
  - Drift policy that gates the job (D4).
  - PDF / HTML evidence rendering (D3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from decoy_engine.quality.diagnostic import compute_diagnostic
from decoy_engine.quality.fidelity import compute_fidelity
from decoy_engine.quality.snapshot import compute_distribution_snapshot

QUALITY_REPORT_SCHEMA_VERSION = "quality-report/v1"

_GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (0.95, "A"),
    (0.85, "B"),
    (0.70, "C"),
    (0.50, "D"),
)


def compute_quality_report(
    source_df: pd.DataFrame,
    output_df: pd.DataFrame,
    *,
    job_id: int | None = None,
    source_fingerprint: str | None = None,
    output_fingerprint: str | None = None,
    joint_columns: list[tuple[str, str]] | None = None,
    expect_row_parity: bool = True,
    null_drift_threshold_pp: float = 10.0,
    sampled: bool = False,
    sample_size: int | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """End-to-end: snapshot both frames, diagnose, score, assemble report.

    Args:
        source_df: Pre-mask / pre-generate frame.
        output_df: Post-mask / post-generate frame.
        job_id: Optional job id to record in the report.
        source_fingerprint / output_fingerprint: Optional SHA-256 (or
            other) fingerprints of the source / output payloads. The
            engine does not compute these; callers (D2 runner) supply
            them so the same fingerprint pinning the manifest evidence
            also pins this report.
        joint_columns: Pairs of column names to snapshot as joints.
            Passed through to `compute_distribution_snapshot` on both
            sides.
        expect_row_parity: True for mask jobs, False for generate.
        null_drift_threshold_pp: Per-column null-rate drift tolerance.
        sampled / sample_size: Set when the caller has down-sampled
            either frame before snapshotting. Recorded in the report
            for the operator to see; not used in scoring.
        now_iso: ISO timestamp for the `generated_at` field. None ->
            current UTC time. Injectable for deterministic tests.

    Returns:
        QualityReport dict matching schema `quality-report/v1`.
    """
    src_snap = compute_distribution_snapshot(
        source_df,
        joint_columns=joint_columns,
    )
    out_snap = compute_distribution_snapshot(
        output_df,
        joint_columns=joint_columns,
    )
    diagnostic = compute_diagnostic(
        src_snap,
        out_snap,
        expect_row_parity=expect_row_parity,
        null_drift_threshold_pp=null_drift_threshold_pp,
    )
    fidelity = compute_fidelity(src_snap, out_snap)
    return assemble_quality_report(
        source_snapshot=src_snap,
        output_snapshot=out_snap,
        diagnostic=diagnostic,
        fidelity=fidelity,
        job_id=job_id,
        source_fingerprint=source_fingerprint,
        output_fingerprint=output_fingerprint,
        sampled=sampled,
        sample_size=sample_size,
        now_iso=now_iso,
    )


def assemble_quality_report(
    *,
    source_snapshot: dict[str, Any],
    output_snapshot: dict[str, Any],
    diagnostic: dict[str, Any],
    fidelity: dict[str, Any],
    job_id: int | None = None,
    source_fingerprint: str | None = None,
    output_fingerprint: str | None = None,
    sampled: bool = False,
    sample_size: int | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Pure assembly: combine pre-computed pieces into a QualityReport.

    Used when the caller has already paid for the snapshot +
    diagnostic + fidelity computations and just wants the framed
    output. D2 job integration is the primary caller.
    """
    overall = fidelity.get("overall_score")
    return {
        "schema_version": QUALITY_REPORT_SCHEMA_VERSION,
        "generated_at": now_iso if now_iso is not None else _now_iso(),
        "job_id": job_id,
        "source": {
            "kind": "job_input" if job_id is not None else "frame",
            "fingerprint": source_fingerprint,
            "row_count": int(source_snapshot.get("row_count", 0)),
        },
        "output": {
            "kind": "job_output" if job_id is not None else "frame",
            "fingerprint": output_fingerprint,
            "row_count": int(output_snapshot.get("row_count", 0)),
        },
        "diagnostic": diagnostic,
        "marginal": fidelity.get("marginal", {}),
        "pairwise": fidelity.get("pairwise", {}),
        "overall_score": overall,
        "grade": score_to_grade(overall),
        "sampled": bool(sampled),
        "sample_size": int(sample_size) if sample_size is not None else None,
        "warnings": _collect_warnings(diagnostic, fidelity),
        "omissions": _collect_omissions(fidelity),
    }


def score_to_grade(score: float | None) -> str:
    """Map an overall similarity score in [0, 1] to a letter grade.

    Score thresholds:
      A: >= 0.95
      B: >= 0.85
      C: >= 0.70
      D: >= 0.50
      F:  < 0.50
      unavailable: score is None.
    """
    if score is None:
        return "unavailable"
    for threshold, grade in _GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


# ── helpers ────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    # tz-aware UTC ISO string. Tests pin via `now_iso=` so this branch
    # only runs in production.
    return datetime.now(timezone.utc).isoformat()


def _collect_warnings(
    diagnostic: dict[str, Any],
    fidelity: dict[str, Any],
) -> list[dict[str, Any]]:
    """Aggregate human-readable warnings from diagnostic + fidelity.

    Each warning is a small dict `{source, code, detail}` so the UI
    can group by source (diagnostic / fidelity) and code (kind_drift,
    null_drift, etc.) without parsing free text.
    """
    warnings: list[dict[str, Any]] = []
    for check in diagnostic.get("checks", []):
        if check.get("passed"):
            continue
        warnings.append(
            {
                "source": "diagnostic",
                "code": check.get("check"),
                "detail": {k: v for k, v in check.items() if k not in ("check", "passed")},
            },
        )
    if fidelity.get("marginal", {}).get("score") is None:
        warnings.append(
            {
                "source": "fidelity",
                "code": "no_comparable_columns",
                "detail": {},
            },
        )
    if fidelity.get("pairwise", {}).get("score") is None and fidelity.get(
        "pairwise",
        {},
    ).get("joints"):
        warnings.append(
            {
                "source": "fidelity",
                "code": "no_comparable_joints",
                "detail": {},
            },
        )
    # Deterministic ordering for byte-stable JSON output.
    warnings.sort(key=lambda w: (w["source"], w["code"]))
    return warnings


def _collect_omissions(fidelity: dict[str, Any]) -> list[dict[str, Any]]:
    """List per-column / per-joint exclusions from the score.

    Excluded entries (comparable: False) are the ones that did not
    contribute to marginal / pairwise aggregates. Reporting them
    explicitly lets the operator see "5 columns were not comparable"
    without re-deriving from the per-column list.
    """
    omissions: list[dict[str, Any]] = []
    for col in fidelity.get("marginal", {}).get("columns", []):
        if not col.get("comparable"):
            omissions.append(
                {
                    "kind": "column",
                    "name": col.get("column"),
                    "reason": col.get("method"),
                },
            )
    for joint in fidelity.get("pairwise", {}).get("joints", []):
        if not joint.get("comparable"):
            omissions.append(
                {
                    "kind": "joint",
                    "name": "+".join(joint.get("columns", [])),
                    "reason": joint.get("method"),
                },
            )
    omissions.sort(key=lambda o: (o["kind"], o["name"] or "", o["reason"] or ""))
    return omissions
