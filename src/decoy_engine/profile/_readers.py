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

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccess, FileHandle, FileRequest, FileRole
from decoy_engine.fileaccess.adapters import arrow as arrow_adapter
from decoy_engine.fileaccess.adapters import pandas as pandas_adapter
from decoy_engine.profile._fixed_width_reader import read_fixed_width_handle

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
class FooterFacts:
    """Everything routing needs about one Parquet file, read from one open handle's footer.

    `null_counts` is `(column, count)` per flat top-level column in schema order; a count is
    `None` when a row group has no null-count statistic, which is not "no nulls"."""

    num_rows: int
    schema: pa.Schema
    null_counts: tuple[tuple[str, int | None], ...]
    row_groups: int
    max_row_group_rows: int


def _footer_null_counts(metadata: pq.FileMetaData) -> dict[str, int | None]:
    """Exact null count per flat top-level column from footer statistics, `None` when any
    non-empty row group lacks one. Nested columns have no single leaf and are omitted."""
    leaf_index = {metadata.schema.column(j).path: j for j in range(metadata.num_columns)}
    counts: dict[str, int | None] = {}
    for name in metadata.schema.to_arrow_schema().names:
        leaf = leaf_index.get(name)
        if leaf is None:
            continue
        total = 0
        complete = True
        for group in range(metadata.num_row_groups):
            if metadata.row_group(group).num_rows == 0:
                continue
            stats = metadata.row_group(group).column(leaf).statistics
            if stats is None or not stats.has_null_count:
                complete = False
                break
            total += stats.null_count
        counts[name] = total if complete else None
    return counts


def footer_facts_of(parquet_file: pq.ParquetFile) -> FooterFacts:
    """The routing facts of an open file, from its own footer (no second open, no data read)."""
    metadata = parquet_file.metadata
    groups = [metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)]
    return FooterFacts(
        num_rows=metadata.num_rows,
        schema=parquet_file.schema_arrow,
        null_counts=tuple(_footer_null_counts(metadata).items()),
        row_groups=len(groups),
        max_row_group_rows=max(groups, default=0),
    )


class OpenedLazyBatches:
    """One open Parquet handle: its footer facts, a batch iterator and the owner of the close.

    `schema`, the facts and `batches` all come from the same handle, so what a caller
    validates is what it reads. `close()` is idempotent and safe to call from a `finally`
    on any exit, including one before the first batch."""

    def __init__(
        self,
        parquet_file: pq.ParquetFile,
        batch_rows: int,
        use_threads: bool,
        handle: FileHandle | None = None,
    ) -> None:
        self._parquet_file = parquet_file
        # The authorized handle lives as long as the batches do, through early failure and
        # interrupted consumption, because a strict handle's descriptor is the file.
        self._handle = handle
        self._closed = False
        self.facts = footer_facts_of(parquet_file)
        self.batches = self._read(batch_rows, use_threads)

    @property
    def schema(self) -> pa.Schema:
        return self.facts.schema

    @property
    def num_rows(self) -> int:
        return self.facts.num_rows

    @property
    def row_groups(self) -> int:
        return self.facts.row_groups

    @property
    def max_row_group_rows(self) -> int:
        return self.facts.max_row_group_rows

    def _read(self, batch_rows: int, use_threads: bool) -> Iterator[pa.RecordBatch]:
        # Wrap pyarrow's own iterator in a Python generator, as `iter_batches` does.
        # Returning the C-level iterator raw makes a downstream `yield from` raise
        # `RuntimeError: generator raised StopIteration` on Python 3.10 for an empty file:
        # the iterator's StopIteration escapes the consuming generator frame (PEP 479).
        if self._handle is not None:
            yield from arrow_adapter.iter_batches(
                self._handle, self._parquet_file, batch_size=batch_rows, use_threads=use_threads
            )
        else:
            yield from self._parquet_file.iter_batches(
                batch_size=batch_rows, use_threads=use_threads
            )

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._parquet_file.close()
            finally:
                if self._handle is not None:
                    self._handle.close()


@dataclass(frozen=True)
class LazySource:
    """A batch-readable handle onto one on-disk Parquet file.

    Holds only a path and the policy it was built under: every read (batches, schema, row
    count) asks that policy for the file fresh, so passing a `LazySource` around never pins
    a resident Arrow table and a later read is judged by the policy that created the source
    even after the entry point returned. Reading is delegated to
    `pyarrow.parquet.ParquetFile.iter_batches`, which decodes one `batch_size` chunk at a
    time rather than the whole file; schema and row count come from the Parquet footer,
    which parsing does not touch column data.

    `scratch=True` marks a file the engine itself wrote into its own scratch space; it is
    read directly and never through a policy.
    """

    path: Path
    access: FileAccess | None = field(default=None, compare=False, repr=False)
    field_key: str = field(default="source.path", compare=False)
    scratch: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if not self.scratch and self.access is None:
            object.__setattr__(self, "access", fileaccess.capture())

    def _handle(self) -> FileHandle:
        if self.scratch:
            return fileaccess.engine_handle(self.path)
        if self.access is None:  # pragma: no cover - set in __post_init__
            raise fileaccess.FileAccessUnavailable("no file access policy")
        return self.access.open_read(
            FileRequest(FileRole.SOURCE, "read", str(self.path), self.field_key)
        )

    def iter_batches(self, batch_rows: int) -> Iterator[pa.RecordBatch]:
        """Yield the file's rows as batches of at most `batch_rows` each."""
        handle = self._handle()
        try:
            parquet_file = arrow_adapter.parquet_file(handle)
            yield from arrow_adapter.iter_batches(handle, parquet_file, batch_size=batch_rows)
        finally:
            handle.close()

    def open_batches(
        self,
        batch_rows: int,
        *,
        pre_buffer: bool | None = None,
        buffer_size: int = 0,
        use_threads: bool = True,
    ) -> OpenedLazyBatches:
        """Open the file once and return its footer facts, a batch iterator and the owner
        of the close, so the schema a caller validates is the schema its batches are read
        under. `schema` then `iter_batches` reopens the file twice and leaves a gap in
        which it can change; a caller that must trust the two agree needs this. The schema
        is real even for a zero-row file, whose iterator yields none.

        `pre_buffer=None` omits the argument, so each installed PyArrow keeps its own
        default (`False` on 24.0.0, `True` on 25.0.1); pass it to pin the behavior.
        `buffer_size` and `use_threads` default to pyarrow's own defaults."""
        options: dict[str, Any] = {}
        if pre_buffer is not None:
            options["pre_buffer"] = pre_buffer
        if buffer_size:
            options["buffer_size"] = buffer_size
        handle = self._handle()
        try:
            parquet_file = arrow_adapter.parquet_file(handle, **options)
        except BaseException:
            handle.close()
            raise
        try:
            return OpenedLazyBatches(parquet_file, batch_rows, use_threads, handle)
        except BaseException:
            parquet_file.close()
            handle.close()
            raise

    def footer_facts(self) -> FooterFacts:
        """Schema, row count, footer null counts and row-group layout from ONE open handle.

        The `schema`, `num_rows` and `column_null_counts` accessors each reopen the file; a
        caller that needs the facts to agree with one another reads them here."""
        handle = self._handle()
        try:
            parquet_file = arrow_adapter.parquet_file(handle)
            try:
                return footer_facts_of(parquet_file)
            finally:
                parquet_file.close()
        finally:
            handle.close()

    @property
    def schema(self) -> pa.Schema:
        """The file's Arrow schema, read from the Parquet footer only."""
        handle = self._handle()
        try:
            return arrow_adapter.parquet_file(handle).schema_arrow
        finally:
            handle.close()

    @property
    def num_rows(self) -> int:
        """The file's total row count, read from the Parquet footer only."""
        handle = self._handle()
        try:
            return int(arrow_adapter.read_metadata(handle).num_rows)
        finally:
            handle.close()

    def column_null_counts(self) -> dict[str, int | None]:
        """Exact null count per flat top-level column, from footer statistics only.

        `None` when any row group lacks a null count for the column (statistics
        written off), so the caller must not read it as "no nulls". A file with no
        rows (no row groups, or only empty ones) has none. Nested columns have no single leaf and are omitted.
        """
        handle = self._handle()
        try:
            return _footer_null_counts(arrow_adapter.read_metadata(handle))
        finally:
            handle.close()

    def to_table(self) -> pa.Table:
        """Read the whole file into memory. Fallback for small jobs only."""
        handle = self._handle()
        try:
            return arrow_adapter.read_table(handle)
        finally:
            handle.close()


class _PolicyBound:
    """What a file source needs to ask the policy it was built under for its file."""

    path: Path
    access: FileAccess | None
    field_key: str

    def _handle(self) -> FileHandle:
        if self.access is None:  # pragma: no cover - set in __post_init__
            raise fileaccess.FileAccessUnavailable("no file access policy")
        return self.access.open_read(
            FileRequest(FileRole.SOURCE, "read", str(self.path), self.field_key)
        )


@dataclass(frozen=True)
class ParquetFileSource(_PolicyBound):
    """`ProfileSource` for a local Parquet file, backed by `LazySource`.

    row_count/schema come from the footer; the bounded sample is the first
    `sample_rows` rows via `iter_batches`. `pd.read_parquet` is never called
    on the whole file on the bounded path (only `to_frame()` reads everything).
    """

    path: Path
    access: FileAccess | None = field(default=None, compare=False, repr=False)
    field_key: str = field(default="source.path", compare=False)

    def __post_init__(self) -> None:
        if self.access is None:
            object.__setattr__(self, "access", fileaccess.capture())

    def _lazy(self) -> LazySource:
        return LazySource(self.path, self.access, self.field_key)

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
class FixedWidthFileSource(_PolicyBound):
    """`ProfileSource` for a local fixed-width file.

    Row count is O(1) and exact: `filesize // bytes-per-record`, where the
    per-record byte length (columns + line terminator) is read from the first
    line. The bounded sample reads only the first `sample_rows` records.
    """

    path: Path
    layout: Any
    access: FileAccess | None = field(default=None, compare=False, repr=False)
    field_key: str = field(default="source.path", compare=False)

    def __post_init__(self) -> None:
        if self.access is None:
            object.__setattr__(self, "access", fileaccess.capture())

    def row_count(self) -> RowCount:
        with self._handle() as handle:
            size = handle.stat().st_size
            with handle.open_binary() as fh:
                first_line = fh.readline()
        if not first_line:
            return RowCount(0, exact=True)
        # A fixed-width file has uniform record bytes (columns + terminator);
        # the first line's byte length divides the file size into records.
        return RowCount(size // len(first_line), exact=True)

    def schema(self) -> pa.Schema:
        with self._handle() as handle:
            one_row = read_fixed_width_handle(handle, self.layout, max_records=1)
        return pa.Table.from_pandas(one_row, preserve_index=False).schema

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        with self._handle() as handle:
            return read_fixed_width_handle(handle, self.layout, max_records=sample_rows)

    def to_frame(self) -> pd.DataFrame:
        with self._handle() as handle:
            return read_fixed_width_handle(handle, self.layout)


@dataclass(frozen=True)
class CsvFileSource(_PolicyBound):
    """`ProfileSource` for a local CSV file.

    CSV has no footer, so the row count is an O(1) byte-size estimate:
    `(filesize - header_bytes) // mean_row_bytes`, with mean row width measured
    from a bounded header sample. The estimate is flagged (`exact=False`). The
    bounded sample is `pd.read_csv(nrows=...)`.
    """

    path: Path
    access: FileAccess | None = field(default=None, compare=False, repr=False)
    field_key: str = field(default="source.path", compare=False)

    def __post_init__(self) -> None:
        if self.access is None:
            object.__setattr__(self, "access", fileaccess.capture())

    def row_count(self) -> RowCount:
        header_bytes = 0
        line_bytes: list[int] = []
        with self._handle() as handle:
            total = handle.stat().st_size
            with handle.open_binary() as fh:
                header = fh.readline()
                header_bytes = len(header)
                for _ in range(_CSV_ESTIMATE_LINES):
                    line = fh.readline()
                    if not line:
                        break
                    line_bytes.append(len(line))
        return estimate_csv_rows(total, header_bytes, line_bytes)

    def schema(self) -> pa.Schema:
        with self._handle() as handle:
            head = pandas_adapter.read_csv(handle, nrows=_CSV_ESTIMATE_LINES)
        return pa.Table.from_pandas(head, preserve_index=False).schema

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        with self._handle() as handle:
            return pandas_adapter.read_csv(handle, nrows=sample_rows)

    def to_frame(self) -> pd.DataFrame:
        with self._handle() as handle:
            return pandas_adapter.read_csv(handle)


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


def build_profile_source(
    descriptor: dict[str, Any], *, field_key: str = "source.path"
) -> ProfileSource:
    """Build the `ProfileSource` for one validated source descriptor. A local file is
    read through the installed policy under `field_key`, the config position it came from.

    Dispatches on `type` (file/s3/gcs) exactly as the old eager loader did.
    Cloud readers are imported lazily so the boto3 / google-cloud SDKs stay
    optional dependencies loaded only when a cloud source is actually used.
    """
    src_type = descriptor.get("type")
    if src_type == "file":
        return _build_file_source(descriptor, field_key)
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


def _build_file_source(descriptor: dict[str, Any], field_key: str) -> ProfileSource:
    fmt = descriptor.get("format")
    path = descriptor.get("path")
    if not isinstance(path, str):
        raise ValueError(f"profile_source: file source missing string `path`, got {path!r}")
    if fmt == "csv":
        return CsvFileSource(Path(path), field_key=field_key)
    if fmt == "parquet":
        return ParquetFileSource(Path(path), field_key=field_key)
    if fmt == "fixed_width":
        layout = descriptor.get("layout")
        if not isinstance(layout, dict):
            raise ValueError(
                f"profile_source: fixed_width file source missing `layout`, got {layout!r}"
            )
        return FixedWidthFileSource(Path(path), layout, field_key=field_key)
    raise NotImplementedError(
        f"profile_source: unsupported file format {fmt!r}. "
        "V1 supports csv | parquet | fixed_width only."
    )


__all__ = [
    "BOUNDED_FALLBACK_ROWS",
    "CsvFileSource",
    "FixedWidthFileSource",
    "FooterFacts",
    "LazySource",
    "OpenedLazyBatches",
    "ParquetFileSource",
    "ProfileSource",
    "RowCount",
    "build_profile_source",
    "estimate_csv_rows",
    "footer_facts_of",
    "head_frame_from_batches",
]
