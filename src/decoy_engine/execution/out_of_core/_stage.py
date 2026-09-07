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
    from collections.abc import Mapping
    from pathlib import Path


class MaskedKeyStager:
    """Tee for one table's rewritten batch stream.

    `add` takes each (pre-reconcile, reconciled) batch pair as it flows to the
    sink: the reconciled key columns are appended to one narrow Parquet file,
    and the pre-reconcile chunk types are recorded in `observed` per column.
    After `close`, `source()` re-reads the staged copy as a `LazySource`.

    `masked_observed_types` (the single-source-read reorder driver): when the
    batches this stager sees have ALREADY been reconciled onto a stable schema
    before it ever gets them (the payload store's Arrow IPC spill fixes one
    schema for its whole stream, so `_stream_driver.py` reconciles before
    storing), `add`'s own per-batch type detection would only ever see that
    one fixed type and lose the narrower value-derived type a table-level
    all-null (or otherwise degenerate) masked column truly has. Passing the
    driver's OWN pre-reconciliation observation here seeds `observed` with the
    true types instead, and `add` skips re-deriving them from the
    (already-uniform) batches it receives.
    """

    def __init__(
        self,
        path: Path,
        *,
        columns: tuple[str, ...],
        fixed_schema: pa.Schema,
        masked_observed_types: Mapping[str, set[pa.DataType]] | None = None,
    ) -> None:
        self._columns = columns
        self._indexes = tuple(fixed_schema.get_field_index(col) for col in columns)
        schema = pa.schema([fixed_schema.field(idx) for idx in self._indexes])
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._schema = schema
        self._writer = pq.ParquetWriter(path, schema)
        if masked_observed_types is None:
            self.observed: dict[str, set[pa.DataType]] = {col: set() for col in columns}
            self._track_observed = True
        else:
            self.observed = {col: set(masked_observed_types.get(col, set())) for col in columns}
            self._track_observed = False
        self.rows = 0

    def add(self, rewritten: pa.RecordBatch, reconciled: pa.RecordBatch) -> None:
        if self._track_observed:
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
