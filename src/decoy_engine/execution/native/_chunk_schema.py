"""Later-chunk schema-drift contract shared by both chunked routes.

Split out of `_dispatch.py` (module-size ratchet, native-throughput program). The
native route admits a table on its FIRST chunk's schema at preflight; nothing else
catches a later chunk drifting (a dropped column, an extra one, or a changed Arrow
type). This guard is a pure, self-contained check with no dependency on the route
orchestrator, so it lives in its own module and `_dispatch` imports it back.
"""

from __future__ import annotations

from collections.abc import Iterator

import pyarrow as pa

from decoy_engine.errors import DecoyError
from decoy_engine.execution._errors import ExecutionError


class NativeChunkSchemaDriftError(ExecutionError, DecoyError):
    """A chunk after the first no longer matches the schema the native route
    admitted at PREFLIGHT: a column went missing, an extra one appeared, or a
    column's Arrow type changed. Preflight only inspects the FIRST chunk (an
    eager check before any masking starts), so nothing upstream of this class
    catches a later chunk drifting -- without it, a missing column is silently
    dropped from the output, an extra one raises an uncoded `KeyError`, and a
    changed type is fed to a kernel that never validated it. Raised BEFORE the
    drifting chunk is yielded (Decision 10): chunks already consumed by the
    caller are unaffected, but there is no reversal of them.
    """

    code: str = "native_chunk_schema_drift"

    def __init__(self, message: str, *, table: str, chunk_index: int, detail: str) -> None:
        super().__init__(code="native_chunk_schema_drift", message=message)
        self.table = table
        self.chunk_index = chunk_index
        self.detail = detail

    def __str__(self) -> str:
        # Callers that map errors by `.code` read `.message`; the bare message keeps
        # `str()` identical to what this class has always printed.
        return self.message


def _drift(table: str, chunk_index: int, message: str, detail: str) -> NativeChunkSchemaDriftError:
    return NativeChunkSchemaDriftError(
        f"{table!r} chunk {chunk_index}: {message}",
        table=table,
        chunk_index=chunk_index,
        detail=detail,
    )


def validate_chunk_schema(
    expected: pa.Schema, chunk: pa.Table, *, table: str, chunk_index: int
) -> None:
    """Raise when `chunk` does not match the first chunk's schema; never cast.

    The one source-drift contract both routes share: a missing or extra column,
    or a column whose Arrow type differs from the first chunk's, raises
    `NativeChunkSchemaDriftError`. The single exception is a `null`-typed column
    (an all-null chunk whose reader had no type to infer), which passes here
    unchanged. Validation and casting are separate steps (`cast_null_columns`) so
    the chunk reaches the ingest guards, and any adapter, exactly as the source
    produced it: a cast to an integer type would make an all-null chunk look like
    a null-bearing int column and refuse input the oracle masks.

    The reverse case (the first chunk's column is `null`-typed and this chunk's is
    typed) raises `ExecutionError(code="chunked_leading_null_type")`: the stream's
    type is fixed by its first chunk, and a writer opened on a `null` field cannot
    accept the later type.
    """
    expected_names = set(expected.names)
    actual_names = set(chunk.schema.names)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise _drift(
            table,
            chunk_index,
            f"schema drift vs the first chunk (missing={missing}, extra={extra})",
            f"missing:{missing};extra:{extra}",
        )
    from decoy_engine.execution._transforms import stored_index_fields

    if stored_index_fields(expected) != stored_index_fields(chunk.schema):
        was = sorted(stored_index_fields(expected))
        now = sorted(stored_index_fields(chunk.schema))
        raise _drift(
            table,
            chunk_index,
            f"stored pandas index fields changed vs the first chunk (was={was}, now={now})",
            f"stored_index:{was}->{now}",
        )
    for name in chunk.schema.names:
        expected_type = expected.field(name).type
        actual_type = chunk.schema.field(name).type
        if actual_type == expected_type:
            continue
        if pa.types.is_null(expected_type):
            raise ExecutionError(
                code="chunked_leading_null_type",
                message=(
                    f"{table!r} chunk {chunk_index}: column {name!r} was null-typed in the "
                    f"first chunk and is {actual_type} here. The stream's type is fixed by "
                    "its first chunk, so the source must supply a fixed schema (declare the "
                    "column's type instead of inferring it per chunk)."
                ),
            )
        if pa.types.is_null(actual_type):
            continue
        raise _drift(
            table,
            chunk_index,
            f"column {name!r} type changed {expected_type} -> {actual_type}",
            f"type_changed:{name}:{expected_type}->{actual_type}",
        )


def cast_null_columns(expected: pa.Schema, chunk: pa.Table) -> pa.Table:
    """`chunk` with each `null`-typed column cast to `expected`'s type.

    Call only on a chunk `validate_chunk_schema` accepted, and only after the
    ingest guards ran on the raw chunk."""
    columns = list(chunk.columns)
    changed = False
    for i, name in enumerate(chunk.schema.names):
        expected_type = expected.field(name).type
        if chunk.schema.field(name).type != expected_type:
            columns[i] = columns[i].cast(expected_type)
            changed = True
    if not changed:
        return chunk
    return pa.Table.from_arrays(
        columns, schema=pa.schema([expected.field(n) for n in chunk.schema.names])
    )


def validated_rest(first: pa.Table, rest: Iterator[pa.Table], *, table: str) -> Iterator[pa.Table]:
    """The chunks after `first`, each validated against `first`'s schema, lazily
    and unchanged, so drift raises before the drifting chunk reaches either
    route's loop."""
    for i, chunk in enumerate(rest, start=1):
        validate_chunk_schema(first.schema, chunk, table=table, chunk_index=i)
        yield chunk
