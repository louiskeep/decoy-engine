"""Schema-drift comparator.

Pure function: takes two snapshots (typically same logical schema in
two different environments - dev vs prod, or two snapshots in time of
the same connector) and returns a structural delta. Row-counts are NOT
compared; that's a Phase-3 toggle. Drift here means "table or column
shape changed."

The change-kind vocabulary mirrors the storm DiffScreen so the UI can
render walks-drift and storm-diff with a single component family.
"""

from __future__ import annotations

from decoy_engine.walks.types import Column, DriftResult, SchemaSnapshot, Table


def compare(a: SchemaSnapshot, b: SchemaSnapshot) -> DriftResult:
    """Diff two snapshots. `a` is treated as the baseline; `b` is the
    "current" / candidate. So a table missing from `b` but present in
    `a` is a `removed_table`.

    Stable ordering of the output (sorted by name) - keeps test
    assertions deterministic and makes UI rendering match between
    re-runs of the same drift walk.
    """
    a_tables: dict[str, Table] = {t.name: t for t in a.tables}
    b_tables: dict[str, Table] = {t.name: t for t in b.tables}

    added = tuple(sorted(b_tables.keys() - a_tables.keys()))
    removed = tuple(sorted(a_tables.keys() - b_tables.keys()))

    changed: list[dict] = []
    for name in sorted(a_tables.keys() & b_tables.keys()):
        changed.extend(_diff_columns(a_tables[name], b_tables[name]))

    return DriftResult(
        added_tables=added,
        removed_tables=removed,
        changed_columns=tuple(changed),
        new_pii=(),
    )


def _diff_columns(before: Table, after: Table) -> list[dict]:
    """Per-column diff for tables that exist in both snapshots.

    Returns one dict per change. Multiple changes on the same column
    produce multiple entries (e.g. a column whose type AND nullability
    both shifted gets two `change_kind` records). That's noisier than
    coalescing, but easier to act on - each row is one fix.
    """
    a_cols: dict[str, Column] = {c.name: c for c in before.columns}
    b_cols: dict[str, Column] = {c.name: c for c in after.columns}

    out: list[dict] = []
    for name in sorted(b_cols.keys() - a_cols.keys()):
        out.append(
            {
                "table": before.name,
                "column": name,
                "change_kind": "added",
            }
        )
    for name in sorted(a_cols.keys() - b_cols.keys()):
        out.append(
            {
                "table": before.name,
                "column": name,
                "change_kind": "removed",
            }
        )
    for name in sorted(a_cols.keys() & b_cols.keys()):
        before_col = a_cols[name]
        after_col = b_cols[name]
        if before_col.data_type != after_col.data_type:
            out.append(
                {
                    "table": before.name,
                    "column": name,
                    "change_kind": "type_changed",
                    "from": before_col.data_type,
                    "to": after_col.data_type,
                }
            )
        if before_col.nullable != after_col.nullable:
            out.append(
                {
                    "table": before.name,
                    "column": name,
                    "change_kind": "nullability_changed",
                    "from": before_col.nullable,
                    "to": after_col.nullable,
                }
            )
        if before_col.is_primary_key != after_col.is_primary_key:
            out.append(
                {
                    "table": before.name,
                    "column": name,
                    "change_kind": "pk_changed",
                    "from": before_col.is_primary_key,
                    "to": after_col.is_primary_key,
                }
            )
    return out
