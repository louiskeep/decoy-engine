"""Later-chunk schema-drift guard for the native route.

Split out of `_dispatch.py` (module-size ratchet, native-throughput program). The
native route admits a table on its FIRST chunk's schema at preflight; nothing else
catches a later chunk drifting (a dropped column, an extra one, or a changed Arrow
type). This guard is a pure, self-contained check with no dependency on the route
orchestrator, so it lives in its own module and `_dispatch` imports it back.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.errors import DecoyError


class NativeChunkSchemaDriftError(DecoyError):
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
        super().__init__(message)
        self.table = table
        self.chunk_index = chunk_index
        self.detail = detail


def _check_chunk_schema_drift(
    expected: pa.Schema, chunk: pa.Table, *, table: str, chunk_index: int
) -> None:
    """Raise `NativeChunkSchemaDriftError` if `chunk` no longer matches
    `expected` (the admitted first chunk's schema): a missing/extra column
    name, or a changed Arrow type on a column present in both."""
    expected_names = set(expected.names)
    actual_names = set(chunk.schema.names)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise NativeChunkSchemaDriftError(
            f"{table!r} chunk {chunk_index}: schema drift vs the admitted first "
            f"chunk (missing={missing}, extra={extra})",
            table=table,
            chunk_index=chunk_index,
            detail=f"missing:{missing};extra:{extra}",
        )
    for name in expected.names:
        expected_type = expected.field(name).type
        actual_type = chunk.schema.field(name).type
        if actual_type != expected_type:
            raise NativeChunkSchemaDriftError(
                f"{table!r} chunk {chunk_index}: column {name!r} type changed "
                f"{expected_type} -> {actual_type}",
                table=table,
                chunk_index=chunk_index,
                detail=f"type_changed:{name}:{expected_type}->{actual_type}",
            )
