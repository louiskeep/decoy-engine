"""FK-integrity validators: fk_intact and no_orphan_children (SP-05 / P5.INFRA.4).

Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG; materialise parent
PK set; resolve child FK values against it.

The two validators implement the parent-first DAG pattern from SDV's
HierarchicalMetaData (HMA1) model, adapted for integrity checking rather
than generation:

  fk_intact
    For every declared FK relationship, every non-null child FK value must
    resolve to an entry in the parent PK set. A null FK is not a broken
    reference (nullable FK columns are a legitimate pattern); only non-null
    values that do not appear in the parent PK set are violations.
    Mirrors the ``FAIL`` branch of the engine's existing orphan-policy
    machinery in ``relationships/_graph.py``.

  no_orphan_children
    For every declared FK relationship, every child row must have a non-null
    FK value. A null FK makes the child an orphan with no resolvable parent.
    This is stricter than fk_intact: fk_intact ignores nulls; this validator
    flags them.

Relationships are read from the ``relationships:`` block in the pipeline
config dict. Each entry is expected to have the shape::

    parent:
      table: <str>
      columns: [<str>, ...]
    children:
      - table: <str>
        columns: [<str>, ...]
    orphan_policy: fail | preserve | warn | remap

Both validators run over every relationship entry in the config regardless
of the ``orphan_policy`` field; the validator config's intent is separate
from the masking-phase orphan policy.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.validators._types import ValidatorFinding

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_relationships(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the list of relationship entries from the pipeline config.

    Each entry has ``parent`` (with ``table`` and ``columns``) and
    ``children`` (a list of dicts with ``table`` and ``columns``).
    Returns an empty list when the config has no ``relationships:`` block.

    Args:
        config: Pipeline config dict (validated dump from PipelineConfig).

    Returns:
        List of relationship entry dicts (possibly empty).
    """
    rels = config.get("relationships") or []
    if not isinstance(rels, list):
        return []
    return [r for r in rels if isinstance(r, dict)]


def _parent_pk_set(
    table: pa.Table,
    columns: list[str],
    parent_table_name: str = "",
) -> set[tuple[Any, ...]]:
    """Build the set of non-null parent PK tuples from a pa.Table.

    Composite PKs are represented as sorted tuples matching the column order
    in ``columns``. Single-column PKs are wrapped in a 1-tuple for
    consistency with composite handling.

    Args:
        table: Parent table (read-only; not mutated).
        columns: List of column names forming the parent PK.
        parent_table_name: Table name for error messages.

    Returns:
        Set of non-null PK tuples present in the parent table.

    Raises:
        ValueError: If any column in ``columns`` is not present in ``table``.
            An absent column silently produces an empty PK set, which would
            make every child FK appear broken (mass false-positive quarantine).
    """
    table_cols = set(table.schema.names)
    missing = [c for c in columns if c not in table_cols]
    if missing:
        raise ValueError(
            f"misconfigured FK validator: column(s) {missing!r} not found in parent "
            f"table {parent_table_name!r}. Available columns: {sorted(table_cols)}"
        )
    col_arrays = [table.column(c).to_pylist() for c in columns]
    if not col_arrays:
        return set()
    n = len(col_arrays[0])
    out: set[tuple[Any, ...]] = set()
    for i in range(n):
        row = tuple(col_arrays[j][i] for j in range(len(col_arrays)))
        if any(v is None for v in row):
            continue  # skip null PK rows (defensive; PKs should be non-null)
        out.add(row)
    return out


def _child_fk_values(
    table: pa.Table,
    columns: list[str],
) -> list[tuple[Any, ...] | None]:
    """Return per-row FK values for a child table.

    Each entry is either a tuple of FK column values (one per FK column),
    or None when all FK columns for that row are null (indicating a nullable
    FK relationship).

    Args:
        table: Child table (read-only; not mutated).
        columns: List of column names forming the child FK.

    Returns:
        List of tuples (or None) with one entry per row.
    """
    col_arrays = [table.column(c).to_pylist() for c in columns if c in table.schema.names]
    if not col_arrays:
        return []
    n = len(col_arrays[0])
    result: list[tuple[Any, ...] | None] = []
    for i in range(n):
        row = tuple(col_arrays[j][i] for j in range(len(col_arrays)))
        # If ALL columns in the FK tuple are null, treat the whole FK as null.
        result.append(None if all(v is None for v in row) else row)
    return result


# ---------------------------------------------------------------------------
# Public validators
# ---------------------------------------------------------------------------


def validate_fk_intact(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
) -> tuple[ValidatorFinding, ...]:
    """fk_intact: every non-null child FK value resolves to a parent PK.

    Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG; materialise
    parent PK set; resolve child FK values against it.

    Reads every relationship declared in ``config["relationships"]``. For each
    child table + FK columns, checks that every non-null FK tuple appears in
    the parent PK set. Returns one ValidatorFinding per child-side violation
    group (all failing row indices for a (child_table, child_cols) pair are
    collected into a single finding).

    Null FK values are NOT flagged by this validator (they indicate nullable
    FK columns; use no_orphan_children to flag them).

    Args:
        outputs: Read-only pipeline outputs keyed by table name.
        entry: Per-validator config block (no extra keys expected here).
        config: Full pipeline config dict; relationships are read from it.

    Returns:
        Tuple of ValidatorFinding for each FK pair that has broken references.
    """
    findings: list[ValidatorFinding] = []
    for rel in _extract_relationships(config):
        parent_info = rel.get("parent") or {}
        parent_table_name: str = parent_info.get("table") or ""
        parent_cols: list[str] = parent_info.get("columns") or []

        children = rel.get("children") or []
        if not isinstance(children, list):
            continue

        parent_table = outputs.get(parent_table_name)
        if parent_table is None:
            raise ValueError(
                f"misconfigured fk_intact validator: parent table {parent_table_name!r} "
                f"not found in outputs. Available tables: {sorted(outputs)}"
            )
        pk_set = _parent_pk_set(parent_table, parent_cols, parent_table_name=parent_table_name)

        for child_info in children:
            if not isinstance(child_info, dict):
                continue
            child_table_name: str = child_info.get("table") or ""
            child_cols: list[str] = child_info.get("columns") or []

            child_table = outputs.get(child_table_name)
            if child_table is None:
                continue

            fk_values = _child_fk_values(child_table, child_cols)
            failing: list[int] = []
            for i, fk in enumerate(fk_values):
                if fk is None:
                    continue  # null FK -> not a broken reference; skip
                if fk not in pk_set:
                    failing.append(i)

            if failing:
                ref = f"{parent_table_name}.{parent_cols} -> {child_table_name}.{child_cols}"
                findings.append(
                    ValidatorFinding(
                        validator="fk_intact",
                        table=child_table_name,
                        column=None,
                        failing_row_indices=tuple(failing),
                        detail=(
                            f"{len(failing)} row(s) in {child_table_name} "
                            f"have FK values not found in {parent_table_name} "
                            f"(relationship: {ref})"
                        ),
                    )
                )

    return tuple(findings)


def validate_no_orphan_children(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
) -> tuple[ValidatorFinding, ...]:
    """no_orphan_children: every child row must have a non-null FK value.

    Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG; child rows with
    null FK values are orphaned children that cannot be joined to any parent.

    Stricter than fk_intact: this validator flags child rows whose FK columns
    are null (i.e. orphaned children). fk_intact ignores null FKs; this
    validator treats them as violations.

    Reads every relationship declared in ``config["relationships"]``. Returns
    one ValidatorFinding per child-side violation group.

    Args:
        outputs: Read-only pipeline outputs keyed by table name.
        entry: Per-validator config block (no extra keys expected here).
        config: Full pipeline config dict; relationships are read from it.

    Returns:
        Tuple of ValidatorFinding for each FK pair that has orphaned children.
    """
    findings: list[ValidatorFinding] = []
    for rel in _extract_relationships(config):
        children = rel.get("children") or []
        if not isinstance(children, list):
            continue

        for child_info in children:
            if not isinstance(child_info, dict):
                continue
            child_table_name: str = child_info.get("table") or ""
            child_cols: list[str] = child_info.get("columns") or []

            child_table = outputs.get(child_table_name)
            if child_table is None:
                continue

            fk_values = _child_fk_values(child_table, child_cols)
            failing: list[int] = [i for i, fk in enumerate(fk_values) if fk is None]

            if failing:
                parent_info = rel.get("parent") or {}
                parent_table_name: str = parent_info.get("table") or ""
                parent_cols: list[str] = parent_info.get("columns") or []
                ref = f"{parent_table_name}.{parent_cols} -> {child_table_name}.{child_cols}"
                findings.append(
                    ValidatorFinding(
                        validator="no_orphan_children",
                        table=child_table_name,
                        column=None,
                        failing_row_indices=tuple(failing),
                        detail=(
                            f"{len(failing)} row(s) in {child_table_name} "
                            f"have null FK values (orphaned children) "
                            f"for relationship {ref}"
                        ),
                    )
                )

    return tuple(findings)
