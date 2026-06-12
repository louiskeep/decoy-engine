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
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from decoy_engine.generation.statistical import load_spec
from decoy_engine.generation.statistical._spec import _load_snapshot
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
) -> list[str]:
    """Score a generated table's statistical columns against their snapshots.

    Args:
        generate_columns: The table's `generate_columns` config entries.
        data: The generated column values, keyed by generated column name.
        table_name: Table name, for the warning text.
        threshold: Warn when a snapshot group's overall score is below this.

    Returns:
        Warning strings, one per snapshot artifact whose generated
        columns score below `threshold`. Empty when the table has no
        statistical columns or every group scores at or above it.
    """
    by_snapshot: dict[str, dict[str, str]] = {}
    for col in generate_columns:
        if col.get("type") != "statistical":
            continue
        spec = load_spec(col)
        group = by_snapshot.setdefault(str(col["snapshot_file"]), {})
        if spec.source_column in group:
            _log.debug(
                "fidelity gate: table %r columns %r and %r both map to source "
                "column %r in %r; scoring the later one only",
                table_name,
                group[spec.source_column],
                spec.column,
                spec.source_column,
                col["snapshot_file"],
            )
        group[spec.source_column] = spec.column

    warnings: list[str] = []
    for snapshot_file, columns in sorted(by_snapshot.items()):
        # Rename generated columns to their source names so
        # compute_fidelity's shared-column intersection lines up with
        # the artifact. _load_snapshot is the cached, schema-checked
        # reader the sampler itself used, so this cannot introduce a
        # second failure mode.
        artifact = _load_snapshot(snapshot_file)
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
            f"snapshot={snapshot_file} overall_score={overall} "
            f"threshold={threshold} worst_columns=[{worst_text}]"
        )
    return warnings


def warn_on_low_fidelity(
    generate_columns: list[dict[str, Any]],
    data: dict[str, list[Any]],
    *,
    table_name: str,
    threshold: float,
) -> None:
    """Run the gate and log each warning. The generate path's one call site."""
    for message in score_generated_fidelity(
        generate_columns, data, table_name=table_name, threshold=threshold
    ):
        _log.warning(message)
