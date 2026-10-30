"""Per-chunk runtime half of the chunked FK gate: the FK column sets each
chunk is checked against, and the lossy-passthrough guard.

`fk_passthrough_columns_for_table` / `reject_lossy_chunked_fk_passthrough`
close MEDIUM #4 (DE-10 rework follow-up, 2026-07-13): `run_mask_pipeline_
chunked` (`_chunked.py`) always threads an EMPTY `RelationshipGraph` into
the pandas adapter (self-masking has no parent-map join), so `_fk_keys.
to_pandas_fk_safe`'s ingestion protection -- keyed off that same runtime
graph -- protects NOTHING on this route. Every OTHER chunk-safe strategy
(hash, fpe, redact, truncate, text_redact, date_shift, bucketize, top_code,
text_mask) re-derives its output rather than preserving the raw key, so an unprotected
float64-on-null ingestion widening never survives to the output for them --
but `passthrough` (identity) preserves the raw key verbatim, so a null-bearing
`passthrough` FK column carrying a value beyond `2**53` silently rounds
through this route's unprotected ingestion, exactly like the pre-DE-10
full-frame/sequential bug. These two functions add a TARGETED runtime guard
(not a blanket null-bearing-int reject, which would also catch legitimate
small-int passthrough jobs): reject only when a `passthrough` FK column here
is null-bearing AND carries a value beyond `2**53`, with the same
`out_of_core_fk_key_dtype_unsupported` code every other unrepresentable-key
shape raises. This guard is INDEPENDENT of `gate_fk_child_edges`'s strategy
allowlist (`_chunked_fk_edge_checks.py`): it reads FK-participant columns straight off `config.
relationships`/`config.tables`, both parent- and child-role, so it still
protects a `passthrough` PARENT key column even after the 2026-09-02
cascade-safety fix narrows the CHILD-edge admission gate to `hash` only (a
`passthrough` FK CHILD column is now rejected earlier still, at that gate,
before this guard ever runs for it).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError

# Mirrors `_fk_keys._EXACT_FLOAT_INT_BOUND`: IEEE-754 double precision (53-bit
# mantissa) is the largest magnitude every integer up to and including it can
# round-trip through float64 exactly -- the bound `reject_lossy_chunked_fk_
# passthrough` uses to decide whether an unprotected ingestion float64-on-null
# widening would actually lose precision for this column's data.
_EXACT_FLOAT_INT_BOUND = 2**53


def _fk_columns_on_table(config: dict[str, Any], table: str) -> set[str]:
    """Every column of `table` that is an FK endpoint, in either role, read off
    `config.relationships` (nested `parent` / `children` entries)."""
    fk_columns: set[str] = set()
    for rel_entry in config.get("relationships") or []:
        if not isinstance(rel_entry, dict):
            continue
        parent_info = rel_entry.get("parent") or {}
        if isinstance(parent_info, dict) and parent_info.get("table") == table:
            fk_columns.update(c for c in parent_info.get("columns") or [] if isinstance(c, str))
        for child_info in rel_entry.get("children") or []:
            if not isinstance(child_info, dict) or child_info.get("table") != table:
                continue
            fk_columns.update(c for c in child_info.get("columns") or [] if isinstance(c, str))
    return fk_columns


def _table_entry(config: dict[str, Any], table: str) -> dict[str, Any] | None:
    return next(
        (t for t in config.get("tables") or [] if isinstance(t, dict) and t.get("name") == table),
        None,
    )


def fk_hash_strategy_columns_for_table(config: dict[str, Any], table: str) -> set[str]:
    """Every FK-participant column on `table` (either endpoint role) declared
    with EXACTLY the `hash` strategy.

    Scopes predicate 12's exact cross-adapter-safe dtype restriction to the
    population the hash-only FK self-mask allowlist (condition (a)) actually
    admits, for the per-chunk REAL-type stage of the check
    (`_chunked_fk_dtype.reject_mismatched_chunked_fk_declared_dtype`). Mirrors
    `fk_passthrough_columns_for_table`'s both-sides scan. A `truncate`/`fpe`/
    `passthrough` FK column elsewhere on the same table still gets the
    (unchanged) coarse family-level guard via `fk_declared_dtypes_for_table`,
    but is NOT held to this narrower exact-dtype rule: predicate 12 is specific
    to the hash-only cascade-safety fix, not every chunk-safe strategy.
    """
    fk_columns = _fk_columns_on_table(config, table)
    if not fk_columns:
        return set()
    table_cfg = _table_entry(config, table)
    if table_cfg is None:
        return set()
    hash_columns: set[str] = set()
    for col in table_cfg.get("columns") or []:
        if not isinstance(col, dict) or col.get("strategy") != "hash":
            continue
        col_name = col.get("name")
        if isinstance(col_name, str) and col_name in fk_columns:
            hash_columns.add(col_name)
    return hash_columns


def fk_passthrough_columns_for_table(config: dict[str, Any], table: str) -> set[str]:
    """Every FK-relevant column on `table` admitted onto the chunked route
    under a `passthrough` strategy -- the MEDIUM #4 / BLOCKER #2 gap set (see
    module docstring).

    Covers BOTH sides of a relationship edge where `table` participates,
    symmetric with `_fk_keys.fk_columns_for_table` (the full-frame/sequential
    equivalent, which protects parent AND child columns identically). A
    `passthrough` PARENT key column is not resolved through a join on this
    route (self-masking has no parent-map lookup), but it goes through the
    SAME unprotected `table.to_pandas()` ingestion as every other table this
    route processes (`run_mask_pipeline_chunked` runs one table at a time,
    each with an empty `RelationshipGraph`) -- the vulnerability is about
    ingestion, not joins, so a chunked PARENT table with a null-bearing
    `passthrough` key beyond `2**53` is exactly as exposed as a child one.
    Restricting this to child columns only (the prior cut) left every
    chunked-route parent key column silently rounding through this same
    float64-on-null path; dennis reproduced `[1.0, None,
    9007199254740992.0]` for a parent-side `passthrough` key.
    `check_chunked_compatibility` requires every admitted FK CHILD edge's
    column to declare a strategy explicitly, but a parent-only table has no
    such requirement, so this reads directly off `config.relationships` /
    `config.tables` rather than the (deliberately empty) runtime
    `RelationshipGraph`.

    Mirrors `gate_fk_child_edges`'s config parsing: `relationships` entries
    nest one `parent` (`{"table": ..., "columns": [...]}`) and a list of
    `children` (same shape each), NOT flat `parent_table`/`child_table` keys.
    """
    fk_columns = _fk_columns_on_table(config, table)
    if not fk_columns:
        return set()
    table_cfg = _table_entry(config, table)
    if table_cfg is None:
        return set()
    passthrough_columns = {
        col.get("name")
        for col in table_cfg.get("columns") or []
        if isinstance(col, dict) and col.get("strategy") == "passthrough"
    }
    return fk_columns & passthrough_columns


def reject_lossy_chunked_fk_passthrough(
    chunk: pa.Table, *, table: str, passthrough_fk_columns: set[str]
) -> None:
    """Fail closed on a null-bearing `passthrough` FK column carrying a key
    beyond `2**53` (MEDIUM #4, see module docstring) instead of letting the
    chunked route's unprotected ingestion silently round it through
    float64-on-null.

    A narrower guard than `execution._guards.reject_null_bearing_int`
    deliberately: that guard rejects ANY null-bearing int column under
    truncate/categorical (those strategies re-derive their output, so
    ANY int+null ambiguity is a cross-substrate correctness question, not a
    magnitude one). `passthrough` preserves the raw value, so a null-bearing
    `passthrough` int column with every value within exact float64 precision
    round-trips losslessly through this route's unprotected ingestion even
    without `to_pandas_fk_safe`'s protection -- rejecting it too would reject
    legitimate small-int passthrough FK jobs for no correctness reason. Only
    the genuinely lossy shape (null-bearing AND a value beyond `2**53`) fails
    closed here.
    """
    for column in passthrough_fk_columns:
        if column not in chunk.column_names:
            continue
        arrow_type = chunk.schema.field(column).type
        if not pa.types.is_integer(arrow_type):
            continue
        arrow_column = chunk.column(column)
        if arrow_column.null_count == 0:
            continue  # null-free integer column never hits the float64 fallback
        bounds = pc.min_max(  # type: ignore[attr-defined, unused-ignore]
            arrow_column, skip_nulls=True
        ).as_py()
        col_min, col_max = bounds["min"], bounds["max"]
        if col_min is None or col_max is None:
            continue  # every value null; nothing to lose
        if col_max > _EXACT_FLOAT_INT_BOUND or col_min < -_EXACT_FLOAT_INT_BOUND:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FK_KEY_DTYPE_UNSUPPORTED,
                message=(
                    f"Column {table}.{column} is a null-bearing passthrough FK "
                    "key column carrying a value beyond exactly-representable "
                    "float64 precision (> 2**53). The chunked route's ingestion "
                    "does not protect this column (self-masking runs with an "
                    "empty RelationshipGraph); preserving it verbatim would "
                    "silently round the key."
                ),
            )
