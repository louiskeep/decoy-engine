"""A pre-resolved column carried through the chunked mask lane.

The mixed route resolves a coupled child's FK key over the WHOLE table, against
the resident generated parent, in one call (`_mixed_fk_bridge`). That keeps the
orphan warning to one per edge and keeps the cross-chunk fold from ever having
to sum orphan counts. The payload columns then mask chunk-by-chunk on the lane
unchanged. This carrier joins the two: it hands each payload chunk the slice of
the resolved key that belongs to the chunk's own rows, in place of the source
key, before the chunk reaches the masker. The column is configured
`passthrough` in the per-table config (`_mixed_static`), so the masker carries
it untouched and no strategy ever sees it.

ALIGNMENT CONTRACT. The resolved array and the payload stream must describe the
same rows in the same order. The carrier is built from the same prepared source
the stream reads (a resident table, or the one Parquet file the lazy stream
opens), and it enforces the contract rather than assuming it:

  * a chunk's own source key must equal the slice of the keys that were
    resolved, row for row (catches a reordered or changed source);
  * the slices advance monotonically with no overlap or gap;
  * the chunk rows must total exactly the resolved cardinality.

Any breach raises `mixed_key_payload_misaligned`. For a lazy source the breach
can only surface mid-stream, after output has begun, so the run aborts through
the publish session instead of declining; the route declines BEFORE publishing
whenever the footer snapshot already disagrees (`_pipeline_mixed_route`).

The wrapper preserves `_FixedSchemaChunks` (the producer type the chunked entry
recognises to admit deterministic Faker over bool/int/uint) by building a new
one whose schema carries the resolved column's type.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.execution._chunked_input import _FixedSchemaChunks
from decoy_engine.execution._errors import ExecutionError

if TYPE_CHECKING:
    pass

__all__ = ["PreResolvedColumn"]


def _misaligned(table: str, detail: str) -> ExecutionError:
    return ExecutionError(
        code=reasons.runtime.MIXED_KEY_PAYLOAD_MISALIGNED,
        message=f"table {table!r}: the resolved FK key does not line up with the payload rows: "
        f"{detail}.",
    )


@dataclass(frozen=True)
class PreResolvedColumn:
    """One column whose chunk values are slices of an array resolved up front.

    `source` is the column as the payload stream will read it (used only for the
    alignment check); `resolved` is the value each row takes in the output."""

    table: str
    name: str
    source: pa.Array
    resolved: pa.Array

    def __post_init__(self) -> None:
        if len(self.source) != len(self.resolved):
            raise _misaligned(self.table, "source and resolved key lengths differ")

    def schema_for(self, source_schema: pa.Schema) -> pa.Schema:
        """`source_schema` with this column's field retyped to the resolved type,
        keeping the field's nullability and metadata."""
        index = source_schema.get_field_index(self.name)
        field = source_schema.field(index)
        if field.type == self.resolved.type:
            return source_schema
        return source_schema.set(index, field.with_type(self.resolved.type))

    def substitute(self, chunk: pa.Table, offset: int, schema: pa.Schema) -> pa.Table:
        """`chunk` (whose rows start at `offset` of the table) with the resolved
        slice in place of the source key, after checking the two line up."""
        count = chunk.num_rows
        if offset + count > len(self.resolved):
            raise _misaligned(self.table, "the payload stream is longer than the resolved key")
        index = chunk.schema.get_field_index(self.name)
        if count and not chunk.column(index).combine_chunks().equals(
            self.source.slice(offset, count)
        ):
            raise _misaligned(
                self.table, f"the source key differs from the resolved rows at offset {offset}"
            )
        return chunk.set_column(index, schema.field(self.name), self.resolved.slice(offset, count))

    def wrap(self, chunks: Iterable[pa.Table]) -> Iterable[pa.Table]:
        """The payload stream with the resolved key substituted per chunk."""
        if isinstance(chunks, _FixedSchemaChunks):
            schema = self.schema_for(chunks.source_schema)
            return _FixedSchemaChunks(schema, lambda: self._iterate(chunks, schema), chunks.close)
        return self._iterate(chunks, None)

    def _iterate(self, chunks: Iterable[pa.Table], schema: pa.Schema | None) -> Iterator[pa.Table]:
        offset = 0
        for chunk in chunks:
            target = schema if schema is not None else self.schema_for(chunk.schema)
            yield self.substitute(chunk, offset, target)
            offset += chunk.num_rows
        if offset != len(self.resolved):
            raise _misaligned(
                self.table,
                f"the payload stream ended at {offset} rows but {len(self.resolved)} were resolved",
            )
