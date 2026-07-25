"""Bounded, metadata-first source readers for profiling.

Pattern: deferred materialization / metadata-first admission (Apache Spark
/ Dask, both Apache-2.0). See: Spark Catalyst logical-plan + action-on-
`.collect()`; Dask lazy graph + `.compute()`; both decide partitioning from
cheap source metadata and materialize only on an action. Parquet footer
metadata read via `pyarrow.parquet.read_metadata(...).num_rows` /
`ParquetFile.schema_arrow` (Apache Arrow, Apache-2.0) touches only the file
footer, not row-group column data; bounded samples come from
`ParquetFile.iter_batches`, Arrow's own row-group/batch streaming.

Why this module exists: `profile_source` used to hand every source to a flat
`pd.read_*`, materializing the whole frame only to then sample ~10k rows from
it. That eager materialization is what lets a large job OOM *inside profiling*,
before the runner-level bounded routing ever runs. The `ProfileSource` protocol
splits the three things profiling actually needs -- a true row count, a schema,
and a bounded sample -- so each can come from cheap metadata plus a
`<= sample_rows` read instead of a full-frame load.

Row-count honesty: Parquet and fixed_width give an exact count in O(1) (footer
`num_rows`; `filesize // record_bytes`). CSV has no footer and newline-counting
is O(bytes) I/O, so its count is an explicit byte-size *estimate*
(`RowCount.exact=False`); downstream admission (SC7b) keys off that flag rather
than trusting a CSV number it cannot cheaply verify.

`LazySource` (previously in `execution/out_of_core/_source.py`) lives here now
as the single lazy-Parquet reader shared by profiling and the out-of-core
runner; the runner imports it through the re-export shim at its old path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.profile._fixed_width_reader import read_fixed_width

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# How many lines to sample when estimating a CSV's mean row width. Bounded so
# the estimate stays cheap even on a large object (local read, or one ranged
# fetch on cloud). Small samples are fine: the estimate is coarse by design.
_CSV_ESTIMATE_LINES = 1_000

# Fallback bounded sample size when a caller asks for a full scan
# (sample_rows=None) under residency="bounded" -- see profile_source. A full
# scan on a source too big to hold is exactly what this module removes, so the
# combination degrades to a bounded scan with a loud warning rather than OOM.
BOUNDED_FALLBACK_ROWS = 10_000


@dataclass(frozen=True)
class RowCount:
    """A table's total row count plus whether that count is exact.

    `exact=True` for Parquet (footer `num_rows`) and fixed_width
    (`filesize // record_bytes`); `exact=False` for CSV, whose count is an
    O(1) byte-size estimate. The flag rides onto `TableProfile.row_count_exact`
    so route admission knows whether it is trusting a footer count or an
    estimate.
    """

    value: int
    exact: bool


@runtime_checkable
class ProfileSource(Protocol):
    """One source, readable as cheap metadata plus a bounded sample.

    Separates the three things profiling needs so a large source is never
    fully materialized just to be sampled. `to_frame()` is the explicit
    opt-out (small-job / residency="full") full read.
    """

    def row_count(self) -> RowCount:
        """Total rows, exact or estimated, from cheap metadata only."""
        ...

    def schema(self) -> pa.Schema:
        """Arrow schema, from footer/header only (no full column scan)."""
        ...

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        """Up to `sample_rows` rows as a DataFrame (bounded read)."""
        ...

    def to_frame(self) -> pd.DataFrame:
        """Full eager read. Opt-out for small jobs / whole-column scans."""
        ...


@dataclass(frozen=True)
class LazySource:
    """A batch-readable handle onto one on-disk Parquet file.

    Holds only a path: every read (batches, schema, row count) opens the file
    fresh, so passing a `LazySource` around never pins a resident Arrow table.
    Reading is delegated to `pyarrow.parquet.ParquetFile.iter_batches`, which
    decodes one `batch_size` chunk at a time rather than the whole file; schema
    and row count come from the Parquet footer, which parsing does not touch
    column data.
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


@dataclass(frozen=True)
class ParquetFileSource:
    """`ProfileSource` for a local Parquet file, backed by `LazySource`.

    row_count/schema come from the footer; the bounded sample is the first
    `sample_rows` rows via `iter_batches`. `pd.read_parquet` is never called
    on the whole file on the bounded path (only `to_frame()` reads everything).
    """

    path: Path

    def _lazy(self) -> LazySource:
        return LazySource(self.path)

    def row_count(self) -> RowCount:
        return RowCount(self._lazy().num_rows, exact=True)

    def schema(self) -> pa.Schema:
        return self._lazy().schema

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        return head_frame_from_batches(
            self._lazy().iter_batches(sample_rows), sample_rows, self._lazy().schema
        )

    def to_frame(self) -> pd.DataFrame:
        return self._lazy().to_table().to_pandas()


@dataclass(frozen=True)
class FixedWidthFileSource:
    """`ProfileSource` for a local fixed-width file.

    Row count is O(1) and exact: `filesize // bytes-per-record`, where the
    per-record byte length (columns + line terminator) is read from the first
    line. The bounded sample reads only the first `sample_rows` records.
    """

    path: Path
    layout: Any

    def row_count(self) -> RowCount:
        size = os.path.getsize(self.path)
        with open(self.path, "rb") as fh:
            first_line = fh.readline()
        if not first_line:
            return RowCount(0, exact=True)
        # A fixed-width file has uniform record bytes (columns + terminator);
        # the first line's byte length divides the file size into records.
        return RowCount(size // len(first_line), exact=True)

    def schema(self) -> pa.Schema:
        one_row = read_fixed_width(str(self.path), self.layout, max_records=1)
        return pa.Table.from_pandas(one_row, preserve_index=False).schema

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        return read_fixed_width(str(self.path), self.layout, max_records=sample_rows)

    def to_frame(self) -> pd.DataFrame:
        return read_fixed_width(str(self.path), self.layout)


@dataclass(frozen=True)
class CsvFileSource:
    """`ProfileSource` for a local CSV file.

    CSV has no footer, so the row count is an O(1) byte-size estimate:
    `(filesize - header_bytes) // mean_row_bytes`, with mean row width measured
    from a bounded header sample. The estimate is flagged (`exact=False`). The
    bounded sample is `pd.read_csv(nrows=...)`.
    """

    path: Path

    def row_count(self) -> RowCount:
        total = os.path.getsize(self.path)
        header_bytes = 0
        line_bytes: list[int] = []
        with open(self.path, "rb") as fh:
            header = fh.readline()
            header_bytes = len(header)
            for _ in range(_CSV_ESTIMATE_LINES):
                line = fh.readline()
                if not line:
                    break
                line_bytes.append(len(line))
        return estimate_csv_rows(total, header_bytes, line_bytes)

    def schema(self) -> pa.Schema:
        head = pd.read_csv(self.path, nrows=_CSV_ESTIMATE_LINES)
        return pa.Table.from_pandas(head, preserve_index=False).schema

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        return pd.read_csv(self.path, nrows=sample_rows)

    def to_frame(self) -> pd.DataFrame:
        return pd.read_csv(self.path)


# ---------------------------------------------------------------------
# Shared helpers (reused by the cloud readers)
# ---------------------------------------------------------------------


def head_frame_from_batches(
    batches: Iterable[pa.RecordBatch], sample_rows: int, schema: pa.Schema
) -> pd.DataFrame:
    """Collect batches until `sample_rows` rows are gathered, as a DataFrame.

    Stops reading as soon as the row target is met, so an arbitrarily large
    source yields only `<= sample_rows` decoded rows. An empty source returns
    an empty frame carrying the schema's columns (so the walker still emits one
    ColumnProfile per column).
    """
    collected: list[pa.RecordBatch] = []
    gathered = 0
    for batch in batches:
        collected.append(batch)
        gathered += batch.num_rows
        if gathered >= sample_rows:
            break
    if not collected:
        return schema.empty_table().to_pandas()
    table = pa.Table.from_batches(collected)
    if table.num_rows > sample_rows:
        table = table.slice(0, sample_rows)
    return table.to_pandas()


def estimate_csv_rows(total_bytes: int, header_bytes: int, line_bytes: list[int]) -> RowCount:
    """Estimate CSV data rows from file size and a bounded line-width sample.

    Divides the post-header byte budget by the mean sampled line width. The
    result is clamped to at least the number of sampled lines so a downstream
    bounded sample can never exceed the reported row count (the ColumnProfile
    `distinct_count <= row_count` invariant). Always flagged `exact=False`.
    """
    sampled = len(line_bytes)
    if sampled == 0:
        # Header-only or empty file: zero data rows (estimate, but confidently so).
        return RowCount(0, exact=False)
    mean_row_bytes = sum(line_bytes) / sampled
    data_bytes = max(total_bytes - header_bytes, 0)
    estimate = int(data_bytes / mean_row_bytes) if mean_row_bytes > 0 else sampled
    return RowCount(max(estimate, sampled), exact=False)


# ---------------------------------------------------------------------
# Factory: descriptor -> ProfileSource
# ---------------------------------------------------------------------


def build_profile_source(descriptor: dict[str, Any]) -> ProfileSource:
    """Build the `ProfileSource` for one validated source descriptor.

    Dispatches on `type` (file/s3/gcs) exactly as the old eager loader did.
    Cloud readers are imported lazily so the boto3 / google-cloud SDKs stay
    optional dependencies loaded only when a cloud source is actually used.
    """
    src_type = descriptor.get("type")
    if src_type == "file":
        return _build_file_source(descriptor)
    if src_type == "s3":
        from decoy_engine.profile._cloud_readers import build_s3_profile_source

        return build_s3_profile_source(descriptor)
    if src_type == "gcs":
        from decoy_engine.profile._cloud_readers import build_gcs_profile_source

        return build_gcs_profile_source(descriptor)
    raise NotImplementedError(
        f"profile_source: unsupported source type {src_type!r}. "
        "Supported types: file, s3, gcs (S18 adds sftp; V2.1 adds db)."
    )


def _build_file_source(descriptor: dict[str, Any]) -> ProfileSource:
    fmt = descriptor.get("format")
    path = descriptor.get("path")
    if not isinstance(path, str):
        raise ValueError(f"profile_source: file source missing string `path`, got {path!r}")
    if fmt == "csv":
        return CsvFileSource(Path(path))
    if fmt == "parquet":
        return ParquetFileSource(Path(path))
    if fmt == "fixed_width":
        layout = descriptor.get("layout")
        if not isinstance(layout, dict):
            raise ValueError(
                f"profile_source: fixed_width file source missing `layout`, got {layout!r}"
            )
        return FixedWidthFileSource(Path(path), layout)
    raise NotImplementedError(
        f"profile_source: unsupported file format {fmt!r}. "
        "V1 supports csv | parquet | fixed_width only."
    )


__all__ = [
    "BOUNDED_FALLBACK_ROWS",
    "CsvFileSource",
    "FixedWidthFileSource",
    "LazySource",
    "ParquetFileSource",
    "ProfileSource",
    "RowCount",
    "build_profile_source",
    "estimate_csv_rows",
    "head_frame_from_batches",
]
