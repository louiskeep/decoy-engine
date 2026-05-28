"""pk_uniqueness scan (engine-v2 S10): every declared-PK column is all-distinct.

Keys off `profile.declared_pk` (the authoritative PK source; PK flags are NOT in
the Plan / ColumnSeed -- Dennis S10 slice-1-2 review ruling b). A PK with any
repeated non-null masked value is a hard job failure (no PK reuse). Nulls are
excluded (a null PK is a separate profile-level concern).
"""

from __future__ import annotations

from decoy_engine.validation.post._scan import ScanContext, ScanOutcome, column_values

_NAME = "pk_uniqueness"


def run_pk_uniqueness(ctx: ScanContext) -> ScanOutcome:
    duplicate_counts: dict[str, int] = {}
    failed = False
    pk_columns = sorted(
        (table.name, col.name)
        for table in ctx.profile.tables
        for col in table.columns
        if col.declared_pk
    )
    for table_name, col_name in pk_columns:
        out_table = ctx.outputs.get(table_name)
        if out_table is None:
            continue
        non_null = [v for v in column_values(out_table, col_name) if v is not None]
        duplicates = len(non_null) - len(set(non_null))
        duplicate_counts[f"{table_name}.{col_name}"] = duplicates
        if duplicates > 0:
            failed = True
    return ScanOutcome(name=_NAME, failed=failed, duplicate_counts=duplicate_counts)
