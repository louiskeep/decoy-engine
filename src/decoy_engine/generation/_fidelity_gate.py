"""Generation-time fidelity warn-gate for statistical columns.

After a generate table builds, every `type: statistical` column is
scored against the distribution snapshot it was sampled from: snapshot
the generated values with the same `compute_distribution_snapshot` the
fit step used, compare with `compute_fidelity` (quantile RMSE + TVD,
SDV QualityReport aggregation; see `quality/fidelity.py` for the
methodology and citations), and surface a warning when the overall
score, or any one comparable column's score, falls below
`global_settings.fidelity_warn_threshold`.

Warn-only by design: the gate never raises on a low score, never
mutates the generated table, and never changes output bytes. The one
signals are the logger (matching the engine's soft-degradation
precedent) and a structured `QualityWarning` record handed to the
caller's `on_warning` sink, which reaches `ExecutionResult.warnings`.
A hard gate is a policy decision deferred to the platform layer.

One broken column can hide in a high aggregate, so the threshold also
applies per comparable column (SDMetrics and Great Expectations report a
table score beside the per-column scores it averages).

Why a gate can score low with a correct sampler: `condition_on`
columns fall back to the marginal when a parent value misses the joint
table, `other_mode: "emit"` introduces the `__other__` token as a
category absent from the source snapshot (it IS fidelity loss against
the source marginal and is counted as such), and tiny `row_count`
values undersample the fitted distribution. The warning names the
worst-scoring columns so the operator can tell which case applies.

Determinism: `compute_distribution_snapshot` and `compute_fidelity`
pin float precision, and statistical sampling is seed-deterministic,
so the same (config, seed, artifact) produces byte-identical warning
strings on every run.

DPS Scope B (guide section 4.8): this module consumes the Plan's
already-pinned statistical specs and snapshot artifacts. It never calls
`_load_snapshot` or opens `source_path` -- `generate_tables` resolves
every `(table, column)` to its `StatisticalSpec` and its full parsed
snapshot artifact once, from `GenerationPlan`, before this gate runs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pandas as pd

from decoy_engine import reasons
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.generation.statistical import StatisticalSpec
from decoy_engine.generation.statistical._spec import StatisticalSpecError
from decoy_engine.quality.fidelity import compute_fidelity
from decoy_engine.quality.snapshot import compute_distribution_snapshot

_log = logging.getLogger(__name__)

DEFAULT_FIDELITY_WARN_THRESHOLD = 0.8


def fidelity_warn_threshold(config: dict[str, Any]) -> float:
    """Read `global_settings.fidelity_warn_threshold` with the model default.

    `generate_tables` accepts unvalidated dicts, so the default must be
    applied here as well as in the `GlobalSettings` model.
    """
    raw = (config.get("global_settings") or {}).get(
        "fidelity_warn_threshold", DEFAULT_FIDELITY_WARN_THRESHOLD
    )
    return float(raw)


def score_generated_fidelity(
    generate_columns: list[dict[str, Any]],
    data: dict[str, list[Any]],
    *,
    table_name: str,
    threshold: float,
    statistical_specs: dict[tuple[str, str], StatisticalSpec],
    snapshot_index_for_column: dict[tuple[str, str], int],
    snapshot_artifacts: list[dict[str, Any]],
) -> list[QualityWarning]:
    """Score a generated table's statistical columns against their snapshots.

    Args:
        generate_columns: The table's `generate_columns` config entries.
        data: The generated column values, keyed by generated column name.
        table_name: Table name, for the warning text.
        threshold: Warn when a snapshot group's overall score, or any comparable
            column's score, is below this. 0.0 disables the gate.
        statistical_specs: `{(table, column): StatisticalSpec}`, pinned at
            compile time (guide section 4.7/4.8) -- no path is reopened.
        snapshot_index_for_column: `{(table, column): index into
            snapshot_artifacts}`, grouping columns that share one artifact
            exactly as the prior path-string grouping did.
        snapshot_artifacts: The Plan's pinned, already-parsed snapshot
            artifacts (`GenerationPlan.snapshots`, decoded once by the
            caller).

    Returns:
        One `QualityWarning` per snapshot artifact whose generated columns score below
        `threshold` overall or in any comparable column. `trigger` is `"overall"` or
        `"column"` (overall wins when both hold). Empty when the table has no statistical
        columns or every score is at or above the threshold.
    """
    by_snapshot: dict[int, dict[str, str]] = {}
    for col in generate_columns:
        if col.get("type") != "statistical":
            continue
        col_name = str(col["name"])
        spec = statistical_specs.get((table_name, col_name))
        index = snapshot_index_for_column.get((table_name, col_name))
        if spec is None or index is None:
            continue  # unreachable through compile_plan; defensive only
        group = by_snapshot.setdefault(index, {})
        if spec.source_column in group:
            _log.debug(
                "fidelity gate: table %r columns %r and %r both map to source "
                "column %r (snapshot index %r); scoring the later one only",
                table_name,
                group[spec.source_column],
                spec.column,
                spec.source_column,
                index,
            )
        group[spec.source_column] = spec.column

    warnings: list[QualityWarning] = []
    for index, columns in sorted(by_snapshot.items()):
        # H5 (guide section 4.7): `index` comes from a pinned `Plan`'s
        # `PinnedStatisticalSpec.snapshot_index`, which a hand-edited or
        # corrupted serialized manifest could set out of range against
        # `GenerationPlan.snapshots`. A raw `IndexError` here is an
        # untyped crash on untrusted input; fail with the same typed,
        # machine-readable-code error family every other malformed-Plan
        # condition in this feature area raises.
        if not 0 <= index < len(snapshot_artifacts):
            raise StatisticalSpecError(
                code=reasons.plan.STATISTICAL_SNAPSHOT_INDEX_OUT_OF_RANGE,
                message=(
                    f"fidelity gate: snapshot_index={index!r} is out of range for "
                    f"{len(snapshot_artifacts)} pinned snapshot artifact(s). The Plan's "
                    "statistical specs disagree with its pinned snapshots -- recompile "
                    "through compile_plan rather than editing a serialized manifest."
                ),
            )
        artifact = snapshot_artifacts[index]
        frame = pd.DataFrame(
            {
                source_col: _frame_column(
                    data[gen_col], statistical_specs.get((table_name, gen_col))
                )
                for source_col, gen_col in sorted(columns.items())
            }
        )
        generated_snapshot = compute_distribution_snapshot(frame)
        fidelity = compute_fidelity(artifact, generated_snapshot)
        overall = fidelity.get("overall_score")
        if overall is None:
            continue
        scored = [
            (c["column"], c["similarity"])
            for c in fidelity["marginal"]["columns"]
            if c["comparable"]
        ]
        below = [(name, score) for name, score in scored if score < threshold]
        if overall >= threshold and not below:
            continue
        worst = sorted(scored, key=lambda item: item[1])[:3]
        warnings.append(
            QualityWarning(
                code=reasons.warning.GENERATION_FIDELITY_BELOW_THRESHOLD,
                provider="statistical",
                column=None,
                detail={
                    "table": table_name,
                    "snapshot_index": index,
                    "overall_score": float(overall),
                    "threshold": float(threshold),
                    "trigger": "overall" if overall < threshold else "column",
                    "worst_columns": [[name, float(score)] for name, score in worst],
                    "below_columns": [[name, float(score)] for name, score in sorted(below)],
                },
            )
        )
    return warnings


def _frame_column(values: list[Any], spec: StatisticalSpec | None) -> Any:
    """Generated cells as a frame column. A day-resolution `date` column would otherwise reach
    the snapshot as strings and score as a kind mismatch against its datetime source. Microsecond
    unit, the unit generation emits: nanoseconds stop at 2262, and a source with a far-future
    sentinel date must still generate."""
    if spec is not None and spec.kind == "datetime":
        return pd.Series(values, dtype="datetime64[us]")
    return values


def _log_line(record: QualityWarning) -> str:
    detail = record.detail
    worst = ",".join(f"{name}:{score}" for name, score in detail["worst_columns"])
    return (
        f"generation_fidelity_below_threshold: table={detail['table']} "
        f"snapshot_index={detail['snapshot_index']} overall_score={detail['overall_score']} "
        f"threshold={detail['threshold']} trigger={detail['trigger']} worst_columns=[{worst}]"
    )


def warn_on_low_fidelity(
    generate_columns: list[dict[str, Any]],
    data: dict[str, list[Any]],
    *,
    table_name: str,
    threshold: float,
    statistical_specs: dict[tuple[str, str], StatisticalSpec],
    snapshot_index_for_column: dict[tuple[str, str], int],
    snapshot_artifacts: list[dict[str, Any]],
    on_warning: Callable[[QualityWarning], None] | None = None,
) -> None:
    """Run the gate, log each warning and hand each record to `on_warning`. The generate path's
    one call site."""
    for record in score_generated_fidelity(
        generate_columns,
        data,
        table_name=table_name,
        threshold=threshold,
        statistical_specs=statistical_specs,
        snapshot_index_for_column=snapshot_index_for_column,
        snapshot_artifacts=snapshot_artifacts,
    ):
        _log.warning(_log_line(record))
        if on_warning is not None:
            on_warning(record)
