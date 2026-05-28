"""sampled_values evidence (engine-v2 S10): synthetic spot-check rows for the manifest.

NOT a pass/fail scan (always `failed=False`) -- it populates the
`quality_summary.sampled_values` evidence block so a reviewer can eyeball the
masked output. SYNTHETIC ONLY (R18 + the operating-model privacy gate): values
are read from the masked OUTPUT of NON-passthrough columns, never from the source,
so no source PII can reach the manifest. Up to `ctx.sample_size` non-null rows per
column (default 100, configurable via `post_validation_sample_size`).
"""

from __future__ import annotations

from typing import Any

from decoy_engine.validation.post._scan import (
    ScanContext,
    ScanOutcome,
    column_values,
    masked_columns,
)

_NAME = "sampled_values"


def run_sampled_values(ctx: ScanContext) -> ScanOutcome:
    sampled: dict[str, list[Any]] = {}
    for table_name, col_name, strategy in masked_columns(ctx.plan):
        if strategy == "passthrough":
            continue  # passthrough echoes the source; excluded so no source PII is sampled
        out_table = ctx.outputs.get(table_name)
        if out_table is None or col_name not in out_table.column_names:
            continue
        non_null = [v for v in column_values(out_table, col_name) if v is not None]
        sampled[f"{table_name}.{col_name}"] = non_null[: ctx.sample_size]
    return ScanOutcome(name=_NAME, failed=False, sampled_values=sampled)
