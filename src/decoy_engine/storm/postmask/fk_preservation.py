"""FK preservation check (Reframe-A).

Walks the configured relationships graph against the MASKED OUTPUT
frames and counts orphans per FK edge. An orphan is a child row whose
FK value does not resolve to any parent-table row's PK value AFTER
masking. Healthy mask jobs should produce zero orphans for every
relationship whose strategies are FK-preserving (hash with shared
namespace, FPE with shared key, passthrough, shuffle, reference).

Orphans can be legitimate when the relationship is tagged
``orphan_policy: skip`` (child rows referencing nonexistent parents
were intentionally dropped). The check honors that policy: an orphan
rate under the tagged tolerance is info; over is the configured
severity.

This check does NOT recompute the relationship graph -- the platform
runner passes in the graph that was built at compile time. We just
re-use it against the output frames.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.storm.postmask.types import FKPreservationFinding, Severity

# Default orphan-rate threshold for relationships without an explicit
# orphan_policy tag. Above 1% = fail; 0.1%-1% = warning; under 0.1% = info.
_DEFAULT_WARNING_THRESHOLD = 0.001  # 0.1%
_DEFAULT_FAIL_THRESHOLD = 0.01  # 1%


def check_fk_preservation(
    output_frames: dict[str, pd.DataFrame],
    config: dict[str, Any],
) -> list[FKPreservationFinding]:
    """Count orphan FKs per declared relationship.

    Args:
        output_frames: ``{table_name: post-mask DataFrame}``.
        config: the validated pipeline config dict.

    Returns:
        List of FKPreservationFinding (one per relationship).
    """
    findings: list[FKPreservationFinding] = []

    for rel in config.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        parent = rel.get("parent", {}) or {}
        children = rel.get("children", []) or []
        namespace = rel.get("namespace") if isinstance(rel.get("namespace"), str) else None
        parent_table = parent.get("table")
        parent_columns = parent.get("columns") or []
        if not isinstance(parent_table, str) or not isinstance(parent_columns, list):
            continue
        if parent_table not in output_frames:
            continue
        parent_df = output_frames[parent_table]

        for child in children:
            if not isinstance(child, dict):
                continue
            child_table = child.get("table")
            child_columns = child.get("columns") or []
            orphan_policy = child.get("orphan_policy")  # e.g. "skip", "warn", "fail"
            if not isinstance(child_table, str) or not isinstance(child_columns, list):
                continue
            if child_table not in output_frames:
                continue
            if len(child_columns) != len(parent_columns):
                # Composite FK length mismatch is a config error caught
                # at validation time; defensive skip here.
                continue
            child_df = output_frames[child_table]

            # Dennis H4 fix (2026-06-01): composite FKs need a
            # tuple-wise containment check, not a per-column check.
            # Per-column: parent has {(1,1), (2,99)} + child row
            # (a=1, b=99) passes column 'a' (parent.a contains 1)
            # AND column 'b' (parent.b contains 99) -- but the TUPLE
            # (1, 99) is a true orphan because no parent row has
            # exactly that combination. Single-column FKs still go
            # through _check_one_fk for the precise per-column
            # diagnostics.
            if len(parent_columns) == 1:
                finding = _check_one_fk(
                    parent_df=parent_df,
                    parent_table=parent_table,
                    parent_col=parent_columns[0],
                    child_df=child_df,
                    child_table=child_table,
                    child_col=child_columns[0],
                    namespace=namespace,
                    orphan_policy=orphan_policy,
                )
                findings.append(finding)
            else:
                finding = _check_composite_fk(
                    parent_df=parent_df,
                    parent_table=parent_table,
                    parent_cols=parent_columns,
                    child_df=child_df,
                    child_table=child_table,
                    child_cols=child_columns,
                    namespace=namespace,
                    orphan_policy=orphan_policy,
                )
                findings.append(finding)

    return findings


def _check_one_fk(
    *,
    parent_df: pd.DataFrame,
    parent_table: str,
    parent_col: str,
    child_df: pd.DataFrame,
    child_table: str,
    child_col: str,
    namespace: str | None,
    orphan_policy: str | None,
) -> FKPreservationFinding:
    """Count orphans for a single (parent.col, child.col) edge."""
    if parent_col not in parent_df.columns or child_col not in child_df.columns:
        return FKPreservationFinding(
            parent_table=parent_table,
            parent_column=parent_col,
            child_table=child_table,
            child_column=child_col,
            severity="error",
            orphan_count=0,
            total_child_rows=len(child_df),
            orphan_rate=0.0,
            namespace=namespace,
            message=(
                f"column {parent_col!r} or {child_col!r} not present in "
                "the masked output -- relationship cannot be walked."
            ),
        )
    # Drop null PKs (parent rows missing the PK aren't joinable anyway)
    # + null FKs (child rows with null FK aren't orphans by definition).
    parent_pks = parent_df[parent_col].dropna()
    child_fks = child_df[child_col].dropna()
    total_child = len(child_fks)
    if total_child == 0:
        return FKPreservationFinding(
            parent_table=parent_table,
            parent_column=parent_col,
            child_table=child_table,
            child_column=child_col,
            severity="info",
            orphan_count=0,
            total_child_rows=0,
            orphan_rate=0.0,
            namespace=namespace,
            message="no non-null FK values in child table; nothing to check.",
        )
    parent_set = set(parent_pks.tolist())
    orphan_mask = ~child_fks.isin(parent_set)
    orphan_count = int(orphan_mask.sum())
    orphan_rate = orphan_count / total_child

    # Severity rules:
    # - 0 orphans = info
    # - under warning threshold = info
    # - between warning + fail = warning
    # - over fail = fail
    # - orphan_policy: skip means orphan-tolerant; demote one notch
    if orphan_count == 0:
        severity: Severity = "info"
        message = "all child FK values resolve."
    elif orphan_rate < _DEFAULT_WARNING_THRESHOLD:
        severity = "info"
        message = f"{orphan_count} orphan(s) below the warning threshold."
    elif orphan_rate < _DEFAULT_FAIL_THRESHOLD:
        severity = "info" if orphan_policy == "skip" else "warning"
        message = (
            f"{orphan_count} orphan(s) ({orphan_rate * 100:.2f}%); orphan_policy={orphan_policy!r}."
        )
    else:
        severity = "warning" if orphan_policy == "skip" else "fail"
        message = (
            f"{orphan_count} orphan(s) ({orphan_rate * 100:.2f}%); above "
            f"the fail threshold; orphan_policy={orphan_policy!r}."
        )

    return FKPreservationFinding(
        parent_table=parent_table,
        parent_column=parent_col,
        child_table=child_table,
        child_column=child_col,
        severity=severity,
        orphan_count=orphan_count,
        total_child_rows=total_child,
        orphan_rate=orphan_rate,
        namespace=namespace,
        message=message,
    )


def _check_composite_fk(
    *,
    parent_df: pd.DataFrame,
    parent_table: str,
    parent_cols: list[str],
    child_df: pd.DataFrame,
    child_table: str,
    child_cols: list[str],
    namespace: str | None,
    orphan_policy: str | None,
) -> FKPreservationFinding:
    """Tuple-wise containment check for composite FKs.

    Dennis H4 fix (2026-06-01). The per-column check called by
    _check_one_fk passes when each column's value exists somewhere in
    the parent's same column, even when the TUPLE never appears as a
    parent row. Composite FKs must check exact tuple containment.

    The finding's parent_column / child_column fields carry a
    comma-joined string of the participating columns so the FE +
    audit doc render the composite identity.
    """
    # The joined string used in the finding's column-name slots. The
    # underlying FKPreservationFinding shape is single-column today
    # (Reframe-A slice 2); we encode the composite by joining.
    parent_col_label = ",".join(parent_cols)
    child_col_label = ",".join(child_cols)

    missing_parent = [c for c in parent_cols if c not in parent_df.columns]
    missing_child = [c for c in child_cols if c not in child_df.columns]
    if missing_parent or missing_child:
        return FKPreservationFinding(
            parent_table=parent_table,
            parent_column=parent_col_label,
            child_table=child_table,
            child_column=child_col_label,
            severity="error",
            orphan_count=0,
            total_child_rows=len(child_df),
            orphan_rate=0.0,
            namespace=namespace,
            message=(
                f"composite FK column(s) missing from masked output: "
                f"parent {missing_parent!r} or child {missing_child!r} "
                "-- relationship cannot be walked."
            ),
        )

    # Drop rows with ANY null in the FK tuple (no SQL FK matches NULL
    # in any component anyway). Same logic on the parent side.
    parent_tuples_df = parent_df[parent_cols].dropna(how="any")
    child_tuples_df = child_df[child_cols].dropna(how="any")
    total_child = len(child_tuples_df)
    if total_child == 0:
        return FKPreservationFinding(
            parent_table=parent_table,
            parent_column=parent_col_label,
            child_table=child_table,
            child_column=child_col_label,
            severity="info",
            orphan_count=0,
            total_child_rows=0,
            orphan_rate=0.0,
            namespace=namespace,
            message="no non-null FK tuples in child table; nothing to check.",
        )

    # QA-4 F3 (2026-06-01): MultiIndex.isin replaces the
    # itertuples + set-membership loop. Pre-fix the pure-Python
    # iteration scaled at O(rows) of Python-level work per child
    # tuple; for child tables >100k rows this dominated the post-mask
    # report time. MultiIndex.isin runs vectorized in pandas/numpy.
    # QA-4 F8 (2026-06-01): size-cap warning. A 10M-row UUID-keyed
    # parent table builds a ~900MB Python set under the old itertuples
    # path. MultiIndex.isin is also O(rows) memory; we cap the parent
    # at 10M rows and emit a warning rather than OOM the worker.
    _PARENT_TUPLE_CAP = 10_000_000
    if len(parent_tuples_df) > _PARENT_TUPLE_CAP:
        return FKPreservationFinding(
            parent_table=parent_table,
            parent_column=parent_col_label,
            child_table=child_table,
            child_column=child_col_label,
            severity="warning",
            orphan_count=0,
            total_child_rows=total_child,
            orphan_rate=0.0,
            namespace=namespace,
            message=(
                f"parent table {parent_table!r} has {len(parent_tuples_df)} rows, "
                f"above the {_PARENT_TUPLE_CAP}-row cap for composite FK orphan "
                "detection. Skipping orphan count; use a sample-based audit "
                "for tables this large."
            ),
        )
    parent_mi = pd.MultiIndex.from_frame(parent_tuples_df)
    child_mi = pd.MultiIndex.from_frame(child_tuples_df)
    orphan_count = int((~child_mi.isin(parent_mi)).sum())
    orphan_rate = orphan_count / total_child

    # Same severity rules as _check_one_fk.
    if orphan_count == 0:
        severity: Severity = "info"
        message = "all child FK tuples resolve."
    elif orphan_rate < _DEFAULT_WARNING_THRESHOLD:
        severity = "info"
        message = f"{orphan_count} orphan tuple(s) below the warning threshold."
    elif orphan_rate < _DEFAULT_FAIL_THRESHOLD:
        severity = "info" if orphan_policy == "skip" else "warning"
        message = (
            f"{orphan_count} orphan tuple(s) ({orphan_rate * 100:.2f}%); "
            f"orphan_policy={orphan_policy!r}."
        )
    else:
        severity = "warning" if orphan_policy == "skip" else "fail"
        message = (
            f"{orphan_count} orphan tuple(s) ({orphan_rate * 100:.2f}%); above "
            f"the fail threshold; orphan_policy={orphan_policy!r}."
        )

    return FKPreservationFinding(
        parent_table=parent_table,
        parent_column=parent_col_label,
        child_table=child_table,
        child_column=child_col_label,
        severity=severity,
        orphan_count=orphan_count,
        total_child_rows=total_child,
        orphan_rate=orphan_rate,
        namespace=namespace,
        message=message,
    )
