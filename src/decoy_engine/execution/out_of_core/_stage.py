"""Narrow on-disk staging of a table's rewritten parent-key columns.

Every outgoing relation must map raw parent keys to the table's FINAL
post-rewrite values, but on the sink path those values exist only while the
rewritten batches flow through to the sink. Staging just the outgoing key
columns to a temp Parquet file and re-reading them batch-wise is the same
bounded-RAM-for-disk-IO trade the runner already makes with its sources
(re-reading spilled intermediates is standard external-memory discipline;
DuckDB's larger-than-memory operators re-read their spilled partitions the
same way), narrowed so the staged copy is key-columns wide, never the table.

The staged file is written under the run's analytically fixed schema (one
Parquet writer, one schema), which erases the per-batch data-derived types a
whole-table build would carry. The stager therefore also records each
column's pre-reconcile chunk types so the runner can recover the data-derived
whole-column type for the relation build, exactly as the resident reassembly
does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution.out_of_core._source import LazySource

if TYPE_CHECKING:
    from pathlib import Path


class MaskedKeyStager:
    """Tee for one table's rewritten batch stream.

    `add` takes each (pre-reconcile, reconciled) batch pair as it flows to the
    sink: the reconciled key columns are appended to one narrow Parquet file,
    and the pre-reconcile chunk types are recorded in `observed` per column.
    After `close`, `source()` re-reads the staged copy as a `LazySource`.
    """

    def __init__(self, path: Path, *, columns: tuple[str, ...], fixed_schema: pa.Schema) -> None:
        self._columns = columns
        self._indexes = tuple(fixed_schema.get_field_index(col) for col in columns)
        schema = pa.schema([fixed_schema.field(idx) for idx in self._indexes])
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._schema = schema
        self._writer = pq.ParquetWriter(path, schema)
        self.observed: dict[str, set[pa.DataType]] = {col: set() for col in columns}
        self.rows = 0

    def add(self, rewritten: pa.RecordBatch, reconciled: pa.RecordBatch) -> None:
        for col in self._columns:
            self.observed[col].add(rewritten.column(rewritten.schema.get_field_index(col)).type)
        self._writer.write_batch(
            pa.record_batch(
                [reconciled.column(idx) for idx in self._indexes],
                schema=self._schema,
            )
        )
        self.rows += reconciled.num_rows

    def close(self) -> None:
        self._writer.close()

    def source(self) -> LazySource:
        return LazySource(self._path)


__all__ = ["MaskedKeyStager"]
