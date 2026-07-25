"""null_audit scan (engine-v2 S10): output null positions match the source.

Masking preserves nulls in place (a null source value stays null; a non-null
value is never turned null, and vice versa). For every masked column the scan
compares the per-row null mask of the output against the source; any positional
mismatch (or a row-count change) is a hard job failure. Records a `NullCount`
(source + output) per column.
"""

from __future__ import annotations

from decoy_engine.validation.post._scan import (
    ScanContext,
    ScanOutcome,
    column_values,
    masked_columns,
)
from decoy_engine.validation.post._types import NullCount

_NAME = "null_audit"


def run_null_audit(ctx: ScanContext) -> ScanOutcome:
    null_counts: dict[str, NullCount] = {}
    failed = False
    for table_name, col_name, _strategy in masked_columns(ctx.plan):
        out_table = ctx.outputs.get(table_name)
        src_table = ctx.sources.get(table_name)
        if out_table is None or src_table is None:
            continue
        if col_name not in out_table.column_names or col_name not in src_table.column_names:
            continue
        out_vals = column_values(out_table, col_name)
        src_vals = column_values(src_table, col_name)
        null_counts[f"{table_name}.{col_name}"] = NullCount(
            source_nulls=sum(1 for v in src_vals if v is None),
            output_nulls=sum(1 for v in out_vals if v is None),
        )
        if len(out_vals) != len(src_vals):
            failed = True  # row count changed -> null positions cannot align
        elif any((o is None) != (s is None) for o, s in zip(out_vals, src_vals, strict=True)):
            failed = True  # a null moved, appeared, or disappeared
    return ScanOutcome(name=_NAME, failed=failed, null_counts=null_counts)
