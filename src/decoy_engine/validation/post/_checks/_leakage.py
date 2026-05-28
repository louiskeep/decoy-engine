"""leakage scan (engine-v2 S10): no source value survives into the masked output.

For every NON-passthrough masked column, no non-null SOURCE value may appear in
the masked output of that column. A passthrough column is excluded (its output
equals the source by design). Any leak is a HARD job failure (privacy is
non-negotiable). The emitted warning carries only a COUNT, never the leaked
values, so the manifest never echoes source PII (R18).
"""

from __future__ import annotations

from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.validation.post._scan import (
    ScanContext,
    ScanOutcome,
    column_values,
    masked_columns,
)

_NAME = "leakage"


def run_leakage(ctx: ScanContext) -> ScanOutcome:
    failed = False
    warnings: list[QualityWarning] = []
    for table_name, col_name, strategy in masked_columns(ctx.plan):
        if strategy == "passthrough":
            continue  # passthrough output == source by design; not a leak
        out_table = ctx.outputs.get(table_name)
        src_table = ctx.sources.get(table_name)
        if out_table is None or src_table is None:
            continue
        source_values = {v for v in column_values(src_table, col_name) if v is not None}
        if not source_values:
            continue
        leaked = {
            v for v in column_values(out_table, col_name) if v is not None and v in source_values
        }
        if leaked:
            failed = True
            warnings.append(
                QualityWarning(
                    code="source_value_leak",
                    provider=strategy,
                    column=col_name,
                    detail={"table": table_name, "leaked_count": len(leaked)},
                )
            )
    return ScanOutcome(name=_NAME, failed=failed, warnings=tuple(warnings))
