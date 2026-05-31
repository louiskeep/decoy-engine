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

from decoy_engine.storm.postmask.types import FKPreservationFinding

# Default orphan-rate threshold for relationships without an explicit
# orphan_policy tag. Above 1% = fail; 0.1%-1% = warning; under 0.1% = info.
_DEFAULT_WARNING_THRESHOLD = 0.001  # 0.1%
_DEFAULT_FAIL_THRESHOLD = 0.01      # 1%


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

            for parent_col, child_col in zip(parent_columns, child_columns):
                finding = _check_one_fk(
                    parent_df=parent_df,
                    parent_table=parent_table,
                    parent_col=parent_col,
                    child_df=child_df,
                    child_table=child_table,
                    child_col=child_col,
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
            total_child_rows=int(len(child_df)),
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
    total_child = int(len(child_fks))
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
        severity = "info"
        message = "all child FK values resolve."
    elif orphan_rate < _DEFAULT_WARNING_THRESHOLD:
        severity = "info"
        message = f"{orphan_count} orphan(s) below the warning threshold."
    elif orphan_rate < _DEFAULT_FAIL_THRESHOLD:
        severity = "info" if orphan_policy == "skip" else "warning"
        message = (
            f"{orphan_count} orphan(s) ({orphan_rate * 100:.2f}%); "
            f"orphan_policy={orphan_policy!r}."
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
