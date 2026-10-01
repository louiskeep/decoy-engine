"""Later-chunk schema-drift contract shared by both chunked routes.

Split out of `_dispatch.py` (module-size ratchet, native-throughput program). The
native route admits a table on its FIRST chunk's schema at preflight; nothing else
catches a later chunk drifting (a dropped column, an extra one, or a changed Arrow
type). This guard is a pure, self-contained check with no dependency on the route
orchestrator, so it lives in its own module and `_dispatch` imports it back.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

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


def conform_chunk_schema(
    expected: pa.Schema, chunk: pa.Table, *, table: str, chunk_index: int
) -> pa.Table:
    """Return `chunk` under the first chunk's schema, or raise on drift.

    The one source-drift contract both routes share: a missing or extra column,
    or a column whose Arrow type differs from the first chunk's, raises
    `NativeChunkSchemaDriftError`. The single exception is a `null`-typed column
    (an all-null chunk whose reader had no type to infer), which is cast to the
    first chunk's type. The cast does not change any value, but it does change the
    column's type, so the per-chunk ingest guards (see `conformed_rest`) must run
    on the chunk as the source produced it, before this cast: an all-null chunk
    cast to an integer type would otherwise look like a null-bearing int column.

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
    columns = list(chunk.columns)
    changed = False
    for i, name in enumerate(chunk.schema.names):
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
            columns[i] = columns[i].cast(expected_type)
            changed = True
            continue
        raise _drift(
            table,
            chunk_index,
            f"column {name!r} type changed {expected_type} -> {actual_type}",
            f"type_changed:{name}:{expected_type}->{actual_type}",
        )
    if not changed:
        return chunk
    return pa.Table.from_arrays(
        columns, schema=pa.schema([expected.field(n) for n in chunk.schema.names])
    )


def conformed_rest(
    first: pa.Table,
    rest: Iterator[pa.Table],
    *,
    table: str,
    ingest_guard: Callable[[pa.Table], None] | None = None,
) -> Iterator[pa.Table]:
    """The chunks after `first`, each conformed to `first`'s schema (see
    `conform_chunk_schema`), lazily, so drift raises before the drifting chunk
    reaches either route's masking loop. `ingest_guard` runs on each chunk as the
    source produced it, before the conform cast."""
    for i, chunk in enumerate(rest, start=1):
        if ingest_guard is not None:
            ingest_guard(chunk)
        yield conform_chunk_schema(first.schema, chunk, table=table, chunk_index=i)
