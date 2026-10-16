"""Canonical Arrow comparison for the Phase F F0 generation oracle (test-side only).

"Byte parity" in F0 means canonical Arrow VALUES + SCHEMA, never serialized artifact bytes.
Two tables are canonically equal when they agree on:

- schema: field names, field order, Arrow types, nullability, per-field metadata and
  schema-level metadata;
- row count and row order;
- every cell value, with null and NaN kept DISTINCT and Python types tagged (an int 1 is not
  a float 1.0 even when the Arrow types were coerced to match).

Chunk layout is NOT part of equality: a column held as one chunk, many chunks, or zero-length
chunks interleaved with real ones canonicalizes to the same token stream. That is the property
F1 needs, because bounded generation emits batches that are concatenated, not one array.

`table_fingerprint` is the checked-in form (sha256 digests); `canonical_diff` is the readable
form used by comparator tests. Neither regenerates expectations: the expected fingerprints live
in `_f0_expected.py`, captured once from the pre-F1 code.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

import pyarrow as pa


def _token(value: Any) -> str:
    """Type-tagged, NaN-aware cell token. Recurses through nested Arrow values."""
    if value is None:
        return "N"
    if isinstance(value, bool):
        return f"b:{int(value)}"
    if isinstance(value, int):
        return f"i:{value}"
    if isinstance(value, float):
        if math.isnan(value):
            return "f:nan"
        return f"f:{value.hex()}"
    if isinstance(value, str):
        return f"s:{len(value)}:{value}"
    if isinstance(value, bytes):
        return f"y:{value.hex()}"
    if isinstance(value, (list, tuple)):
        return "L[" + ",".join(_token(v) for v in value) + "]"
    if isinstance(value, dict):
        return "D{" + ",".join(f"{_token(k)}={_token(v)}" for k, v in value.items()) + "}"
    return f"o:{type(value).__name__}:{value!r}"


def _field_signature(field: pa.Field) -> str:
    meta = field.metadata or {}
    meta_sig = ",".join(f"{k.hex()}={v.hex()}" for k, v in sorted(meta.items()))
    return f"{field.name}|{field.type}|{int(field.nullable)}|{meta_sig}"


def schema_signature(schema: pa.Schema) -> str:
    meta = schema.metadata or {}
    meta_sig = ",".join(f"{k.hex()}={v.hex()}" for k, v in sorted(meta.items()))
    return "\n".join([*(_field_signature(f) for f in schema), f"#schema-meta:{meta_sig}"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def column_tokens(column: pa.Array | pa.ChunkedArray) -> list[str]:
    """Cell tokens in row order, independent of chunk layout."""
    chunks = column.chunks if isinstance(column, pa.ChunkedArray) else [column]
    out: list[str] = []
    for chunk in chunks:
        out.extend(_token(v) for v in chunk.to_pylist())
    return out


def column_digest(column: pa.Array | pa.ChunkedArray) -> str:
    return _sha("\n".join(column_tokens(column)))


def table_fingerprint(table: pa.Table) -> dict[str, Any]:
    """The checked-in oracle form: schema digest, per-column digests, row count, table digest."""
    schema_digest = _sha(schema_signature(table.schema))
    # An ordered list of (name, digest) pairs: column ORDER is part of the contract and a dict
    # would lose it as soon as the checked-in literal is key-sorted.
    columns = [(name, column_digest(table.column(i))) for i, name in enumerate(table.column_names)]
    table_digest = _sha("|".join([schema_digest, str(table.num_rows), *(d for _, d in columns)]))
    return {
        "schema": schema_digest,
        "rows": table.num_rows,
        "columns": columns,
        "table": table_digest,
    }


def canonical_diff(expected: pa.Table, actual: pa.Table) -> list[str]:
    """Human-readable differences, empty when canonically equal."""
    diffs: list[str] = []
    if schema_signature(expected.schema) != schema_signature(actual.schema):
        diffs.append(
            f"schema differs:\n  expected={schema_signature(expected.schema)!r}\n"
            f"  actual={schema_signature(actual.schema)!r}"
        )
    if expected.num_rows != actual.num_rows:
        diffs.append(f"row count {expected.num_rows} != {actual.num_rows}")
    if expected.column_names != actual.column_names:
        diffs.append(f"column order {expected.column_names} != {actual.column_names}")
        return diffs
    for name in expected.column_names:
        left = column_tokens(expected.column(name))
        right = column_tokens(actual.column(name))
        if left != right:
            first = next(
                (i for i, (a, b) in enumerate(zip(left, right, strict=False)) if a != b), -1
            )
            diffs.append(f"column {name!r} differs (first differing row: {first})")
    return diffs


def fingerprints_diff(
    expected: dict[str, Any], actual: dict[str, Any], *, label: str = ""
) -> list[str]:
    """Differences between two `table_fingerprint` dicts, naming the columns that moved."""
    diffs: list[str] = []
    if expected["schema"] != actual["schema"]:
        diffs.append(f"{label}schema digest differs")
    if expected["rows"] != actual["rows"]:
        diffs.append(f"{label}rows {expected['rows']} != {actual['rows']}")
    want = [tuple(p) for p in expected["columns"]]
    got = [tuple(p) for p in actual["columns"]]
    if [n for n, _ in want] != [n for n, _ in got]:
        diffs.append(f"{label}column set/order {[n for n, _ in want]} != {[n for n, _ in got]}")
    got_by_name = dict(got)
    for name, digest in want:
        if got_by_name.get(name) != digest:
            diffs.append(f"{label}column {name!r} values differ")
    if not diffs and expected["table"] != actual["table"]:
        diffs.append(f"{label}table digest differs")
    return diffs


def rechunked(table: pa.Table, sizes: list[int]) -> pa.Table:
    """The same table re-laid-out as chunks of the given row counts (a layout-only change)."""
    assert sum(sizes) == table.num_rows
    batches: list[pa.Table] = []
    start = 0
    for size in sizes:
        batches.append(table.slice(start, size))
        start += size
    return pa.concat_tables(batches)


def assert_matches_oracle(fid: str, actual: dict[str, pa.Table]) -> None:
    """The F1/F3 entry point: `actual` must be canonically identical to the checked-in oracle.

    `actual` maps table name to table for fixture `fid` (any chunk layout). Table set and order
    must match as well, because `generate_tables` returns tables in topological order.
    """
    from tests.parity._f0_expected import EXPECTED

    expected = EXPECTED[fid]
    assert list(actual) == list(expected), (
        f"{fid}: table set/order {list(actual)} != oracle {list(expected)}"
    )
    problems: list[str] = []
    for name, want in expected.items():
        problems += fingerprints_diff(
            want, table_fingerprint(actual[name]), label=f"{fid}.{name}: "
        )
    assert not problems, "\n".join(problems)
