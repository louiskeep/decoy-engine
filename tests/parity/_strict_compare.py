"""Strict per-executor Arrow comparator for the Phase D parity contract.

The value-normalized FK harness (`test_out_of_core_fk_parity.py`) deliberately
folds NaN to null, ignores Arrow width, and lets `1 == 1.0`. That is right for
"same logical data" and wrong for "a kernel slice changed nothing observable".
This module is the STRICT view: each dimension is its own independent check, so
a caller can pin exactly which dimension agrees and which does not, instead of
one opaque pass/fail.

Comparison pattern: pyarrow's own `Schema.equals(..., check_metadata=...)` and
`Array`/compute primitives (`is_null`, `is_nan`), the library's documented
equality API, rather than a hand-rolled field walk. Only the cell token below is
local, because pyarrow has no "NaN equals NaN at the same position, null never
equals NaN, int never equals float" equality.

Every check returns a bool and never raises on a mismatch, so the baseline
tests assert hardcoded literal reports against the actual one. An expectation
must never be computed from the output under test.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc

DIMENSIONS = (
    "schema_structure",
    "field_metadata",
    "type_width",
    "column_order",
    "row_count",
    "row_set",
    "row_order",
    "cellwise_values",
    "null_vs_nan",
)


def _is_nan(value: object) -> bool:
    return isinstance(value, float) and value != value


def _cell_token(value: object) -> tuple[object, ...]:
    """Strict cell identity: null, NaN and the Python type are all distinguished.

    NaN gets one token so two NaNs at the same position compare equal (an
    unchanged NaN must not look like drift); null and NaN never share a token;
    the type name keeps int 1 apart from float 1.0 and bool True.
    """
    if value is None:
        return ("null",)
    if _is_nan(value):
        return ("nan",)
    return (type(value).__name__, value)


def _columns(table: pa.Table) -> dict[str, list[tuple[object, ...]]]:
    return {
        name: [_cell_token(v) for v in table.column(name).to_pylist()]
        for name in table.column_names
    }


def _rows(table: pa.Table, names: list[str]) -> list[tuple[tuple[object, ...], ...]]:
    cols = _columns(table)
    return list(zip(*(cols[n] for n in names), strict=True)) if names else []


def schema_structure_equal(a: pa.Table, b: pa.Table) -> bool:
    """Names, order, exact types (width) and nullability; metadata excluded."""
    return bool(a.schema.equals(b.schema, check_metadata=False))


def field_metadata_equal(a: pa.Table, b: pa.Table) -> bool:
    """Schema-level and per-field key/value metadata, byte-exact."""
    if (a.schema.metadata or {}) != (b.schema.metadata or {}):
        return False
    if len(a.schema) != len(b.schema):
        return False
    return all(
        (fa.metadata or {}) == (fb.metadata or {})
        for fa, fb in zip(a.schema, b.schema, strict=True)
    )


def type_width_equal(a: pa.Table, b: pa.Table) -> bool:
    """Every column present in both has the identical Arrow type (int64 vs
    float64, string vs large_string); a differing column set also fails."""
    if set(a.column_names) != set(b.column_names):
        return False
    return all(a.schema.field(n).type == b.schema.field(n).type for n in a.column_names)


def column_order_equal(a: pa.Table, b: pa.Table) -> bool:
    return a.column_names == b.column_names


def row_count_equal(a: pa.Table, b: pa.Table) -> bool:
    return a.num_rows == b.num_rows


def row_set_equal(a: pa.Table, b: pa.Table) -> bool:
    """Same rows regardless of order (multiset), strict cell identity."""
    if set(a.column_names) != set(b.column_names):
        return False
    names = sorted(a.column_names)
    return sorted(_rows(a, names), key=repr) == sorted(_rows(b, names), key=repr)


def row_order_equal(a: pa.Table, b: pa.Table) -> bool:
    """Same rows in the same sequence. A pure reordering fails this while
    `row_set_equal` still holds; a changed value fails both."""
    if not row_set_equal(a, b):
        return False
    names = sorted(a.column_names)
    return _rows(a, names) == _rows(b, names)


def cellwise_values_equal(a: pa.Table, b: pa.Table) -> bool:
    """Position-by-position cell equality by column name under strict cell
    tokens: same-position NaN equal, null != NaN, int != float."""
    if set(a.column_names) != set(b.column_names) or a.num_rows != b.num_rows:
        return False
    ca, cb = _columns(a), _columns(b)
    return all(ca[n] == cb[n] for n in a.column_names)


def null_vs_nan_equal(a: pa.Table, b: pa.Table) -> bool:
    """For each float column in both tables, the null positions AND the NaN
    positions match. Non-float columns have no NaN concept and pass."""
    if a.num_rows != b.num_rows:
        return False
    for name in set(a.column_names) & set(b.column_names):
        ta, tb = a.schema.field(name).type, b.schema.field(name).type
        if not (pa.types.is_floating(ta) and pa.types.is_floating(tb)):
            continue
        ca, cb = a.column(name), b.column(name)
        if pc.is_null(ca).to_pylist() != pc.is_null(cb).to_pylist():
            return False
        nan_a = pc.fill_null(pc.is_nan(ca), False).to_pylist()
        nan_b = pc.fill_null(pc.is_nan(cb), False).to_pylist()
        if nan_a != nan_b:
            return False
    return True


_CHECKS = {
    "schema_structure": schema_structure_equal,
    "field_metadata": field_metadata_equal,
    "type_width": type_width_equal,
    "column_order": column_order_equal,
    "row_count": row_count_equal,
    "row_set": row_set_equal,
    "row_order": row_order_equal,
    "cellwise_values": cellwise_values_equal,
    "null_vs_nan": null_vs_nan_equal,
}


def compare_strict(oracle: pa.Table, ooc: pa.Table) -> dict[str, bool]:
    """Per-dimension strict agreement report, keys in `DIMENSIONS` order."""
    return {dim: _CHECKS[dim](oracle, ooc) for dim in DIMENSIONS}


def metadata_keys(table: pa.Table) -> list[bytes]:
    """Sorted schema-metadata keys, for pinning the metadata delta as a literal."""
    return sorted((table.schema.metadata or {}).keys())


def schema_signature(table: pa.Table) -> str:
    """`name:type` pairs joined by commas, for pinning a schema as a literal."""
    return ",".join(f"{f.name}:{f.type}" for f in table.schema)
