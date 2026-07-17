"""Fit-time categorical-retention warn-gate (HC-5 D3).

Mirrors `generation/_fidelity_gate.py`'s shape: a pure scorer plus a
logger-only wrapper, never raising, never mutating the snapshot it scores,
never changing bytes. The gate exists because `compute_distribution_snapshot`
(quality/snapshot.py) silently destroys categorical fidelity two ways:

1. Cardinality cliff: an object/string column with >30 distinct values is
   reclassified `categorical -> freetext`; freetext generation is
   LENGTH-ONLY, so the entire value vocabulary and frequency shape are
   lost. Every `freetext`-kind column in a snapshot got there via this
   cliff (see `quality.snapshot._stats_for`), so it always scores 0.0
   here -- including a genuinely free-form column (notes, comments) where
   length-only surrogate text is the intended, correct behavior. The
   warning is a visibility signal ("this column's real vocabulary and
   frequencies are gone from the artifact"), not a defect report; an
   operator scanning fit output uses it to decide whether a column is
   actually a structured code that should opt into `high_cardinality`.
2. Top-K tail collapse: even under the 30-distinct cap, only the top
   `categorical_top_k` values (default 20) are kept in `top_values`; the
   rest collapse into `other_count`. A column's retention score is the
   fraction of its non-null mass the kept `top_values` still cover.

A `high_cardinality: true` column (HC-5) retains its full vocabulary with
`other_count` always 0, so it always scores 1.0 here -- the gate is
reporting that the opt-in delivered on its promise, not flagging it.

This module intentionally has NO dependency on `generation/`: it scores a
`distribution-snapshot/v1` dict, nothing else. Per D3, it must NOT be
called from inside `compute_distribution_snapshot` (that function is
shared by reporting and generation, and must stay warn-free); the single
call site is `decoy fit` (CLI, deferred -- see
docs/backlog/hc-5-cli-fit-wiring.md).
"""

from __future__ import annotations

import logging
from typing import Any

from decoy_engine.quality.snapshot import _CATEGORICAL_DISTINCT_CAP

_log = logging.getLogger(__name__)

DEFAULT_CATEGORICAL_RETENTION_WARN_THRESHOLD = 0.8


def categorical_retention_warn_threshold(config: dict[str, Any]) -> float:
    """Read `global_settings.categorical_retention_warn_threshold` with the
    model default. Fit-time callers accept unvalidated dicts, so the
    default must be applied here as well as in the `GlobalSettings` model.
    """
    raw = (config.get("global_settings") or {}).get(
        "categorical_retention_warn_threshold", DEFAULT_CATEGORICAL_RETENTION_WARN_THRESHOLD
    )
    return float(raw)


def score_categorical_retention(
    snapshot: dict[str, Any],
    *,
    threshold: float,
) -> list[str]:
    """Score a `distribution-snapshot/v1` dict's categorical columns and
    joint tables for retained mass, and return one warning string per
    entry that scores below `threshold`.

    A `threshold` of 0.0 silences every warning: the lowest possible score
    (the cardinality-cliff case) is exactly 0.0, and `score < threshold`
    is never true when both sides are 0.0.

    Args:
        snapshot: A `distribution-snapshot/v1` dict, as produced by
            `compute_distribution_snapshot`. Not mutated.
        threshold: Warn when a column's or joint's retained-mass score is
            below this value.

    Returns:
        Warning strings, one per column/joint below threshold. Empty when
        nothing scores below it.
    """
    warnings: list[str] = []
    columns = snapshot.get("columns") or {}
    for name in sorted(columns):
        col = columns[name]
        kind = col.get("kind")
        if kind == "freetext":
            # Cliff score is always exactly 0.0 (D3); `score < threshold`
            # reduces to `threshold > 0.0` here, but stated the same way as
            # every other branch so the "threshold 0.0 disables everything"
            # contract has one shape to reason about, not a special case.
            if threshold > 0.0:
                warnings.append(
                    f"categorical_retention_cardinality_cliff: column={name!r} score=0.0 "
                    f"threshold={threshold} distinct_count={col.get('distinct_count')} "
                    f"cardinality_cap={_CATEGORICAL_DISTINCT_CAP} "
                    f"(column exceeded the cap and fell to freetext; its real vocabulary "
                    f"is not in the snapshot)"
                )
            continue
        if kind != "categorical":
            continue
        stats = col.get("stats") or {}
        non_null_count = col.get("non_null_count") or 0
        if non_null_count <= 0:
            continue
        retained = sum(int(v.get("count") or 0) for v in stats.get("top_values") or [])
        score = retained / non_null_count
        if score < threshold:
            warnings.append(
                f"categorical_retention_below_threshold: column={name!r} score={score} "
                f"threshold={threshold} other_count={stats.get('other_count')} "
                f"non_null_count={non_null_count}"
            )

    for joint in snapshot.get("joints") or []:
        cols = joint.get("columns") or []
        cells = joint.get("cells") or []
        other_count = int(joint.get("other_count") or 0)
        retained = sum(int(c.get("count") or 0) for c in cells)
        total = retained + other_count
        if total <= 0:
            continue
        score = retained / total
        if score < threshold:
            warnings.append(
                f"categorical_retention_joint_collapse: columns={cols} score={score} "
                f"threshold={threshold} other_count={other_count} total={total}"
            )
    return warnings


def warn_on_low_categorical_retention(
    snapshot: dict[str, Any],
    *,
    threshold: float,
) -> None:
    """Run the gate and log each warning. The `decoy fit` path's one call site."""
    for message in score_categorical_retention(snapshot, threshold=threshold):
        _log.warning(message)
