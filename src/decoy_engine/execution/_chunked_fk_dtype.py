"""Runtime FK-key declared-vs-real dtype guard for chunked execution (DE-10
residual, LOW).

`_chunked_fk.gate_fk_child_edges` condition (f) admits a value-sensitive FK edge
onto the chunked self-masking route purely on the operator-DECLARED `dtype`
strings -- the chunked route masks the child's OWN value with no parent-map
lookup and never sees the parent's data, so at compile time that declaration is
a TRUSTED assertion. A MISdeclaration (declared int64 on FK key columns whose
real data is string/float) passes that gate yet makes the child self-mask a
different byte sequence than the parent (masked elsewhere from its own real
dtype) for the same logical key, silently voiding referential integrity.

Every OTHER route validates the FK key dtype off the REAL Arrow data
(`_fk_keys.to_pandas_fk_safe`, `out_of_core/_join.cast_fk_chunk`). These two
functions close the same gap on the chunked route without trusting the config
string: `fk_declared_dtypes_for_table` surfaces the declarations the gate
trusted, and `reject_mismatched_chunked_fk_declared_dtype` validates each
against the chunk's real Arrow dtype family and fails closed on a cross-family
disagreement. Extracted into this sibling (rather than growing `_chunked_fk.py`
past the orchestration LOC cap) alongside the module it guards.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.execution._chunked_fk import (
    _DECIMAL_UNPROVABLE_FAMILY,
    CHUNK_SAFE_STRATEGIES,
    DTYPE_INVARIANT_STRATEGIES,
    _dtype_family,
)
from decoy_engine.execution._errors import ExecutionError


def _arrow_dtype_family(arrow_type: pa.DataType) -> str:
    """Coarse dtype family for a REAL Arrow type, aligned with `_dtype_family`.

    Resolves a dictionary-encoded column to its value type first (a
    low-cardinality string FK key read from Parquet arrives as
    ``dictionary<values=string, ...>``; its logical family is ``string``, not a
    literal ``dictionary<...>`` string that would spuriously mismatch a declared
    ``string``), then reuses `_dtype_family`'s string mapping so the declared and
    real sides are compared on one identical family scale.
    """
    if pa.types.is_dictionary(arrow_type):
        arrow_type = arrow_type.value_type
    return _dtype_family(str(arrow_type))


def fk_declared_dtypes_for_table(config: dict[str, Any], table: str) -> dict[str, str]:
    """Map each value-sensitive FK key column on `table` (either side of an edge
    it participates in) to the dtype string the CONFIG declared for it.

    `gate_fk_child_edges` condition (f) admits a value-sensitive FK edge purely
    on the operator-DECLARED `dtype` -- so this surfaces exactly those trusted
    declarations for `reject_mismatched_chunked_fk_declared_dtype` to validate
    against the real Arrow dtype. Mirrors `fk_passthrough_columns_for_table`'s
    both-sides symmetry: a PARENT key column self-masks on this route too, off
    the same one-table-at-a-time ingestion, so its declared dtype is just as
    trusted as a child's. `redact` is excluded: it is dtype-invariant (condition
    (f) skips it), so a declared/real dtype disagreement cannot change its
    constant masked output.
    """
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
    if not fk_columns:
        return {}
    table_cfg = next(
        (t for t in config.get("tables") or [] if isinstance(t, dict) and t.get("name") == table),
        None,
    )
    if table_cfg is None:
        return {}
    declared: dict[str, str] = {}
    for col in table_cfg.get("columns") or []:
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        if name not in fk_columns:
            continue
        strategy = col.get("strategy")
        # Only value-sensitive chunk-safe strategies gate on declared dtype;
        # redact is dtype-invariant (condition (f) skips it), so its declared
        # dtype -- if any -- is not a referential-integrity assertion.
        if strategy not in CHUNK_SAFE_STRATEGIES or strategy in DTYPE_INVARIANT_STRATEGIES:
            continue
        dtype = col.get("dtype")
        if isinstance(dtype, str):
            declared[str(name)] = dtype
    return declared


def reject_mismatched_chunked_fk_declared_dtype(
    chunk: pa.Table, *, table: str, declared_fk_dtypes: dict[str, str]
) -> None:
    """Fail closed when a chunk's REAL FK key dtype family disagrees with the
    dtype the config DECLARED for that column (DE-10 residual, LOW).

    A MISdeclaration (declared int64, real data string/float) passes the
    compile-time gate yet makes the child self-mask a different byte sequence
    than the parent -- masked elsewhere from its own real dtype -- for the same
    logical key, silently voiding referential integrity. This validates the
    trusted declaration against the real Arrow dtype at the chunk boundary and
    fails closed on a family mismatch, mirroring how the full-frame / sequential
    / out-of-core routes validate FK key dtypes off real data rather than
    trusting a config string. Family granularity (not exact dtype) matches the
    gate's own tolerance: int32 vs int64 reproduce identical masked bytes, so
    only a cross-FAMILY disagreement (int declared, string/float real) breaks RI.
    """
    for column, declared_dtype in declared_fk_dtypes.items():
        if column not in chunk.column_names:
            continue
        real_type = chunk.schema.field(column).type
        # An all-null column can arrive as Arrow `null` type when no real dtype
        # survived (e.g. an in-memory all-None array). Null keys mask to null on
        # both sides regardless of declared dtype, so RI is trivially preserved
        # and a CORRECT declaration must not be false-positive rejected here. A
        # typed all-null column keeps its real family and is still validated.
        if pa.types.is_null(real_type):
            continue
        real_family = _arrow_dtype_family(real_type)
        declared_family = _dtype_family(declared_dtype)
        # Defense-in-depth: a bare/unprovable decimal DECLARATION fails closed
        # regardless of the real family. The chunked route cannot verify that
        # parent and child declare the same decimal scale (it sees one table at
        # a time), and scale changes the canonical bytes, so a bare decimal FK
        # key can never preserve RI. This does not depend on the real data
        # resolving away from the sentinel, so it holds even for any decimal
        # str form the scale regex might not parse concretely.
        if declared_family == _DECIMAL_UNPROVABLE_FAMILY:
            raise ExecutionError(
                code="chunked_fk_declared_dtype_mismatch",
                message=(
                    f"Column {table}.{column} declares an UNPROVABLE FK key dtype "
                    f"{declared_dtype!r}: a bare decimal/numeric with no precision "
                    "and scale. Decimal scale changes the canonical byte sequence "
                    "and the chunked route cannot verify parent/child scale "
                    "agreement, so a bare decimal FK key cannot preserve "
                    "referential integrity. Declare the exact decimal type "
                    "(e.g. decimal128(P,S)), or use run_pipeline / run_sequential."
                ),
            )
        if real_family != declared_family:
            raise ExecutionError(
                code="chunked_fk_declared_dtype_mismatch",
                message=(
                    f"Column {table}.{column} declares FK key dtype "
                    f"{declared_dtype!r} (family {declared_family!r}) but the real "
                    f"chunk data is {real_type} (family {real_family!r}). The "
                    "chunked FK self-masking gate admits a value-sensitive FK edge "
                    "on the DECLARED dtype alone (it never sees the parent's data); "
                    "a misdeclared dtype lets the child self-mask a different byte "
                    "sequence than the parent for the same logical key, silently "
                    "breaking referential integrity. Declare the FK key column's "
                    "actual dtype, or use run_pipeline / run_sequential (which "
                    "normalize FK key equality via the parent map)."
                ),
            )
