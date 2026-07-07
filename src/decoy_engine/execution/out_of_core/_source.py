"""Path-based lazy source abstraction for out-of-core execution.

Wraps a Parquet file path so a source table can be read as a bounded sequence
of batches instead of held fully resident. Reading is delegated to
`pyarrow.parquet.ParquetFile.iter_batches`, which decodes one `batch_size`
chunk at a time rather than the whole file; this is the established
streaming-read pattern for Parquet (Apache Arrow's own row-group/batch reader,
not a bespoke chunker). Schema and row count come from the file's Parquet
footer metadata, which parsing does not touch column data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class LazySource:
    """A batch-readable handle onto one on-disk Parquet file.

    Holds only a path: every read (batches, schema, row count) opens the file
    fresh, so passing a `LazySource` around never pins a resident Arrow table.
    """

    path: Path

    def iter_batches(self, batch_rows: int) -> Iterator[pa.RecordBatch]:
        """Yield the file's rows as batches of at most `batch_rows` each."""
        parquet_file = pq.ParquetFile(self.path)
        yield from parquet_file.iter_batches(batch_size=batch_rows)

    @property
    def schema(self) -> pa.Schema:
        """The file's Arrow schema, read from the Parquet footer only."""
        return pq.ParquetFile(self.path).schema_arrow

    @property
    def num_rows(self) -> int:
        """The file's total row count, read from the Parquet footer only."""
        return pq.read_metadata(self.path).num_rows

    def to_table(self) -> pa.Table:
        """Read the whole file into memory. Fallback for small jobs only."""
        return pq.read_table(self.path)


__all__ = ["LazySource"]
