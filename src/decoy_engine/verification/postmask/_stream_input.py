"""One table as re-readable, column-projected Arrow batches.

A post-scan check reads a table the way the run holds it: a resident `pa.Table` or a lazy
Parquet or CSV source. `TableBatches` hides the difference, so a check asks for named columns in
bounded batches and never needs the whole table.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.profiling import KeyBatches
from decoy_engine.profiling._scan_integrity import _BATCH_ROWS

if TYPE_CHECKING:
    # Imported for typing only: decoy_engine.profile imports the profiling package.
    from decoy_engine.profile._readers import LazySource, OpenedLazyBatches


@dataclass(frozen=True)
class TableBatches:
    """A table's name, schema and row count, and a way to read named columns in batches.

    `read(columns, batch_rows)` can be called again: each call starts a new read."""

    name: str
    schema: pa.Schema
    num_rows: int
    read: Callable[[Sequence[str], int], Iterator[pa.RecordBatch]]
    bounded: bool = True
    # Set only for a resident table, whose model passes read it whole.
    resident: pa.Table | None = None


def from_table(name: str, table: pa.Table) -> TableBatches:
    def read(columns: Sequence[str], batch_rows: int) -> Iterator[pa.RecordBatch]:
        yield from table.select(list(columns)).to_batches(max_chunksize=batch_rows)

    return TableBatches(name, table.schema, table.num_rows, read, resident=table)


def from_lazy(
    name: str,
    source: LazySource,
    *,
    on_open: Callable[[OpenedLazyBatches], None] | None = None,
) -> TableBatches:
    """Each read opens the source once, calls `on_open` before the first batch (a place to
    compare the opened facts with a routing snapshot) and closes on every exit."""
    facts = source.footer_facts()

    def read(columns: Sequence[str], batch_rows: int) -> Iterator[pa.RecordBatch]:
        opened = source.open_batches(batch_rows, columns=list(columns))
        try:
            if on_open is not None:
                on_open(opened)
            yield from opened.batches
        finally:
            opened.close()

    return TableBatches(name, facts.schema, facts.num_rows, read)


def missing_columns(table: TableBatches, columns: Sequence[str]) -> tuple[str, ...]:
    return tuple(c for c in columns if c not in table.schema.names)


def key_batches(table: TableBatches, columns: Sequence[str]) -> KeyBatches:
    """The `columns` of `table` labelled `k0..kn` in the order given. They must exist."""
    cols = list(columns)
    typed = pa.schema(
        [(f"k{n}", table.schema.field(c).type) for n, c in enumerate(cols)],
    )

    def batches() -> Iterator[pa.RecordBatch]:
        for batch in table.read(cols, _BATCH_ROWS):
            arrays = [batch.column(batch.schema.get_field_index(c)) for c in cols]
            yield pa.RecordBatch.from_arrays(arrays, schema=typed)

    return KeyBatches(typed, batches, table.bounded)
