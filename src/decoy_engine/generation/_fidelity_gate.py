"""Generation-time fidelity warn-gate for statistical columns.

After a generate table builds, every `type: statistical` column is
scored against the distribution snapshot it was sampled from: snapshot
the generated values with the same `compute_distribution_snapshot` the
fit step used, compare with `compute_fidelity` (quantile RMSE + TVD,
SDV QualityReport aggregation; see `quality/fidelity.py` for the
methodology and citations), and surface a warning when the overall
score falls below `global_settings.fidelity_warn_threshold`.

Warn-only by design: the gate never raises on a low score, never
mutates the generated table, and never changes output bytes. The one
signal channel is the logger, matching the engine's established
soft-degradation precedent (numexpr fallback logging). A hard gate is
a policy decision deferred to the platform layer.

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
from typing import Any

import pandas as pd

from decoy_engine.generation.statistical import StatisticalSpec
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
) -> list[str]:
    """Score a generated table's statistical columns against their snapshots.

    Args:
        generate_columns: The table's `generate_columns` config entries.
        data: The generated column values, keyed by generated column name.
        table_name: Table name, for the warning text.
        threshold: Warn when a snapshot group's overall score is below this.
        statistical_specs: `{(table, column): StatisticalSpec}`, pinned at
            compile time (guide section 4.7/4.8) -- no path is reopened.
        snapshot_index_for_column: `{(table, column): index into
            snapshot_artifacts}`, grouping columns that share one artifact
            exactly as the prior path-string grouping did.
        snapshot_artifacts: The Plan's pinned, already-parsed snapshot
            artifacts (`GenerationPlan.snapshots`, decoded once by the
            caller).

    Returns:
        Warning strings, one per snapshot artifact whose generated
        columns score below `threshold`. Empty when the table has no
        statistical columns or every group scores at or above it.
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

    warnings: list[str] = []
    for index, columns in sorted(by_snapshot.items()):
        artifact = snapshot_artifacts[index]
        frame = pd.DataFrame(
            {source_col: data[gen_col] for source_col, gen_col in sorted(columns.items())}
        )
        generated_snapshot = compute_distribution_snapshot(frame)
        fidelity = compute_fidelity(artifact, generated_snapshot)
        overall = fidelity.get("overall_score")
        if overall is None or overall >= threshold:
            continue
        scored = [
            (c["column"], c["similarity"])
            for c in fidelity["marginal"]["columns"]
            if c["comparable"]
        ]
        worst = sorted(scored, key=lambda item: item[1])[:3]
        worst_text = ",".join(f"{name}:{score}" for name, score in worst)
        warnings.append(
            f"generation_fidelity_below_threshold: table={table_name} "
            f"snapshot_index={index} overall_score={overall} "
            f"threshold={threshold} worst_columns=[{worst_text}]"
        )
    return warnings


def warn_on_low_fidelity(
    generate_columns: list[dict[str, Any]],
    data: dict[str, list[Any]],
    *,
    table_name: str,
    threshold: float,
    statistical_specs: dict[tuple[str, str], StatisticalSpec],
    snapshot_index_for_column: dict[tuple[str, str], int],
    snapshot_artifacts: list[dict[str, Any]],
) -> None:
    """Run the gate and log each warning. The generate path's one call site."""
    for message in score_generated_fidelity(
        generate_columns,
        data,
        table_name=table_name,
        threshold=threshold,
        statistical_specs=statistical_specs,
        snapshot_index_for_column=snapshot_index_for_column,
        snapshot_artifacts=snapshot_artifacts,
    ):
        _log.warning(message)
