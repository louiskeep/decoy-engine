"""Diagnostic structural checks between source and output snapshots.

V2 Phase 3 Distribution Integrity, Sprint D1b.

The diagnostic asks "do the two snapshots even make sense to compare?"
It is gating logic for D1c fidelity scoring: if structural prerequisites
fail (column dropped, dtype flipped, null rate moved 50pp), comparing
quantile-by-quantile or category-by-category is meaningless and the
report should say so before showing a number.

Inputs are two snapshot dicts produced by
`decoy_engine.quality.snapshot.compute_distribution_snapshot`. Diagnostic
does not see raw frames; everything it checks must already be captured
in the snapshot shape. That keeps the comparator stateless and lets the
job runner (D2) snapshot once per frame and pass dicts around.

Checks landed in D1b:

  1. column_survival -- every source column appears in output.
     Missing columns fail the check. Added columns are recorded but
     do not fail (pipelines can derive columns; that's a config
     decision, not a structural error).

  2. row_count -- output row count equals source row count, unless the
     caller explicitly opts out via `expect_row_parity=False`. The
     mask path preserves rows; the generate path can change row count
     by design, so the gate is configurable.

  3. kind_drift -- the snapshot "kind" tag (numeric / categorical /
     datetime / freetext / empty / bool) for each surviving column
     matches between source and output. A numeric column whose output
     came back as freetext is almost certainly a strategy
     misconfiguration; that needs to surface immediately.

  4. null_drift -- per-column null percentage shifted by more than
     `null_drift_threshold` (default 10 percentage points). Hash,
     redact, faker, and the like should preserve null rate; a 50pp
     shift means the strategy is dropping or fabricating values.

Out of scope for D1b (later sub-sprints own these):
  - Distribution-shape comparison (mean/quantile/bin drift). That is
    fidelity scoring; D1c owns it.
  - Per-strategy expected-preservation bands. Different strategies
    have different acceptable diagnostic profiles (e.g. shuffle
    preserves row count and null count but rearranges; that's still
    a pass). Strategy-aware policy is D4.
  - Referential integrity (foreign key parity). The snapshot does not
    capture FK structure, so this check would need a different
    primitive; it belongs in the equivalent integrity package, not
    here.

Hard requirements (enforced by tests):
  - Deterministic: same input snapshots + same kwargs -> byte-identical
    JSON output. Check order is stable; per-check details lists are
    sorted by column name.
  - JSON-serializable.
  - Pure: never mutates either input snapshot.
"""

from __future__ import annotations

from typing import Any

QUALITY_DIAGNOSTIC_SCHEMA_VERSION = "quality-diagnostic/v1"

# Default null-rate drift tolerance, in percentage points (not ratio).
# 10pp is generous enough that small fluctuations from row-count
# rounding do not trip the gate, but tight enough that a strategy
# silently dropping 30% of rows is caught. D4 will let callers tighten
# or loosen this per strategy.
_DEFAULT_NULL_DRIFT_PP = 10.0


def compute_diagnostic(
    source_snapshot: dict[str, Any],
    output_snapshot: dict[str, Any],
    *,
    expect_row_parity: bool = True,
    null_drift_threshold_pp: float = _DEFAULT_NULL_DRIFT_PP,
) -> dict[str, Any]:
    """Compare two distribution snapshots for structural validity.

    Args:
        source_snapshot: Snapshot of the pre-mask / pre-generate frame.
        output_snapshot: Snapshot of the post-mask / post-generate frame.
        expect_row_parity: True for mask jobs (rows must match); False
            for generate jobs (row count is a config decision, not a
            structural property).
        null_drift_threshold_pp: Max allowed change in per-column null
            percentage, in percentage points. Per-column changes beyond
            this fail the null_drift check. Default 10pp.

    Returns:
        A dict matching schema `quality-diagnostic/v1`. The `passed`
        field is True iff every check in `checks` is `passed: True`.
    """
    source_cols = source_snapshot.get("columns", {})
    output_cols = output_snapshot.get("columns", {})
    surviving = sorted(set(source_cols.keys()) & set(output_cols.keys()))

    checks = [
        _check_column_survival(source_cols, output_cols),
        _check_row_count(
            source_snapshot,
            output_snapshot,
            expect_parity=expect_row_parity,
        ),
        _check_kind_drift(source_cols, output_cols, surviving=surviving),
        _check_null_drift(
            source_cols,
            output_cols,
            surviving=surviving,
            threshold_pp=null_drift_threshold_pp,
        ),
    ]

    return {
        "schema_version": QUALITY_DIAGNOSTIC_SCHEMA_VERSION,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


# ── individual checks ───────────────────────────────────────────────────────


def _check_column_survival(
    source_cols: dict[str, Any],
    output_cols: dict[str, Any],
) -> dict[str, Any]:
    src_set = set(source_cols.keys())
    out_set = set(output_cols.keys())
    missing = sorted(src_set - out_set)
    added = sorted(out_set - src_set)
    # Added columns are recorded but do NOT fail the check. A derived
    # column is a config decision, not a structural defect; the
    # operator may legitimately add a hash-id column during masking.
    return {
        "check": "column_survival",
        "passed": len(missing) == 0,
        "missing_columns": missing,
        "added_columns": added,
    }


def _check_row_count(
    source_snapshot: dict[str, Any],
    output_snapshot: dict[str, Any],
    *,
    expect_parity: bool,
) -> dict[str, Any]:
    src_rows = int(source_snapshot.get("row_count", 0))
    out_rows = int(output_snapshot.get("row_count", 0))
    if expect_parity:
        passed = src_rows == out_rows
    else:
        # Generate jobs: the check is informational. We still report the
        # numbers so the operator can see what happened, but we do not
        # fail the diagnostic on it.
        passed = True
    return {
        "check": "row_count",
        "passed": passed,
        "expect_parity": expect_parity,
        "source_rows": src_rows,
        "output_rows": out_rows,
    }


def _check_kind_drift(
    source_cols: dict[str, Any],
    output_cols: dict[str, Any],
    *,
    surviving: list[str],
) -> dict[str, Any]:
    drifted: list[dict[str, Any]] = []
    for col in surviving:
        src_kind = source_cols[col].get("kind")
        out_kind = output_cols[col].get("kind")
        # An "empty" output is treated as drift only if the source was
        # non-empty. A column that started empty and stayed empty is
        # not a kind change; it's just no data on either side.
        if src_kind != out_kind:
            drifted.append(
                {
                    "column": col,
                    "source_kind": src_kind,
                    "output_kind": out_kind,
                },
            )
    return {
        "check": "kind_drift",
        "passed": len(drifted) == 0,
        "drifted": drifted,
    }


def _check_null_drift(
    source_cols: dict[str, Any],
    output_cols: dict[str, Any],
    *,
    surviving: list[str],
    threshold_pp: float,
) -> dict[str, Any]:
    drifted: list[dict[str, Any]] = []
    for col in surviving:
        src_pct = _null_pct(source_cols[col])
        out_pct = _null_pct(output_cols[col])
        if src_pct is None or out_pct is None:
            continue
        delta_pp = abs(out_pct - src_pct)
        if delta_pp > threshold_pp:
            drifted.append(
                {
                    "column": col,
                    "source_null_pct": round(src_pct, 4),
                    "output_null_pct": round(out_pct, 4),
                    "delta_pp": round(delta_pp, 4),
                },
            )
    return {
        "check": "null_drift",
        "passed": len(drifted) == 0,
        "threshold_pp": threshold_pp,
        "drifted": drifted,
    }


# ── helpers ─────────────────────────────────────────────────────────────────


def _null_pct(col_snapshot: dict[str, Any]) -> float | None:
    """Return null percentage in [0, 100], or None if the column has no rows.

    A zero-row column has no meaningful null rate; skip rather than
    surface a 0/0 = NaN, which would also break JSON serialization.
    """
    null_count = int(col_snapshot.get("null_count", 0))
    non_null_count = int(col_snapshot.get("non_null_count", 0))
    total = null_count + non_null_count
    if total == 0:
        return None
    return (null_count / total) * 100.0
