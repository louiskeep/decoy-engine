"""The bounded streaming reader: Parquet cutting and CSV batching by bytes.

Promoted from the platform's `api/jobs/native_reader.py` (parity E2a) so both
consumers chunk a source the same way. Established patterns: Arrow's guidance
for bounded Parquet reading is to read by row group and batch and to keep
repeated strings dictionary-encoded until they are needed (``read_dictionary``);
Spark bounds a partition by bytes (``spark.sql.files.maxPartitionBytes``) and
Hadoop record readers frame records before parsing them. Each chunk handed on
owns its buffers (no zero-copy slice of a larger decoded batch), so a small
chunk never keeps its parent batch alive.

Only the caller's `ChunkBudget` is consulted; nothing here reads settings,
prices a source or checks a fingerprint (the platform does that around it).
CSV values arrive as strings with pandas' default NA set treated as null, the
contract the full-frame CSV read keeps (`PANDAS_DEFAULT_NA_VALUES`).
"""

from __future__ import annotations

import io
import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq

from decoy_engine.io import csv_framer as framer

# pandas' default `na_values`, the set the full-frame CSV read (`read_csv(dtype=str)`)
# treats as null; the chunked read must match it for CSV->CSV byte parity.
PANDAS_DEFAULT_NA_VALUES: tuple[str, ...] = (
    "", "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan", "1.#IND",
    "1.#QNAN", "<NA>", "N/A", "NA", "NULL", "NaN", "None", "n/a", "nan", "null",
)

# 4 bytes of offset plus one validity byte per variable-width value; one validity
# byte per fixed-width value. Conservative against Arrow's bit-packed validity so
# the budget is never under-counted.
_VAR_OVERHEAD = 5
_FIXED_OVERHEAD = 1
_STRING_TYPES = ("string", "large_string", "binary", "large_binary")
_pc: Any = pc


class BoundedReadError(RuntimeError):
    """A coded bounded-read failure. ``code`` is stable; the message carries
    identifiers and positions, never row data."""

    code = "bounded_read_error"

    def __init__(self, **detail: Any) -> None:
        text = ", ".join(f"{k}={v!r}" for k, v in detail.items())
        super().__init__(f"{self.code} ({text})" if text else self.code)
        self.detail = detail


class RowExceedsMaxRowBytes(BoundedReadError):  # noqa: N818 - coded condition
    code = "row_exceeds_max_row_bytes"


class CsvRecordExceedsMaxRowBytes(BoundedReadError):  # noqa: N818 - coded condition
    code = "csv_record_exceeds_max_row_bytes"


class CsvMalformed(BoundedReadError):  # noqa: N818 - coded condition
    code = "csv_malformed"


@dataclass(frozen=True)
class ChunkBudget:
    """What bounds one table's chunks: decoded input bytes plus a per-row output
    allowance per chunk, a row cap per chunk, a per-row byte cap, and the Parquet
    batch size to decode at a time."""

    chunk_budget_bytes: int
    max_chunk_rows: int
    max_row_bytes: int
    out_row_bound: int = 0
    read_rows: int = 65_536

    def __post_init__(self) -> None:
        for name in ("chunk_budget_bytes", "max_chunk_rows", "max_row_bytes", "read_rows"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"ChunkBudget.{name} must be a positive int; got {value!r}")
        if isinstance(self.out_row_bound, bool) or not isinstance(self.out_row_bound, int) or self.out_row_bound < 0:
            raise ValueError(f"ChunkBudget.out_row_bound must be a non-negative int; got {self.out_row_bound!r}")


def _binary_length(array: pa.Array) -> pa.Array:
    return _pc.binary_length(array)


def own_array(array: pa.Array) -> pa.Array:
    """A copy of ``array`` in newly allocated buffers. A dictionary array is
    decoded to its value type."""
    if isinstance(array, pa.DictionaryArray):
        return array.dictionary_decode()
    return pa.concat_arrays([array])


def _column_costs(column: pa.Array) -> np.ndarray:
    """Exact decoded bytes per row for one column of a read batch."""
    if isinstance(column, pa.DictionaryArray):
        lengths = pc.take(_binary_length(column.dictionary), column.indices)
        costs = pc.fill_null(lengths, 0).to_numpy(zero_copy_only=False).astype(np.int64)
        return costs + _VAR_OVERHEAD
    if str(column.type) in _STRING_TYPES:
        costs = pc.fill_null(_binary_length(column), 0).to_numpy(zero_copy_only=False)
        return costs.astype(np.int64) + _VAR_OVERHEAD
    try:
        width = max(1, column.type.bit_width // 8)
    except ValueError:
        width = 8
    return np.full(len(column), width + _FIXED_OVERHEAD, dtype=np.int64)


def cut_batch(batch: pa.RecordBatch, budget: ChunkBudget, *, table: str, row_base: int = 0) -> Iterator[pa.Table]:
    """Cut one decoded batch into owned chunks within `budget`; a row over
    `max_row_bytes` raises after the rows before it were yielded."""
    rows = batch.num_rows
    if rows == 0:
        return
    in_cost = np.zeros(rows, dtype=np.int64)
    for column in batch.columns:
        in_cost += _column_costs(column)
    oversized = np.flatnonzero(in_cost > budget.max_row_bytes)
    limit = int(oversized[0]) if len(oversized) else rows
    total = in_cost + budget.out_row_bound
    cumulative = np.cumsum(total)
    start = 0
    while start < limit:
        base = int(cumulative[start - 1]) if start else 0
        end = int(np.searchsorted(cumulative, base + budget.chunk_budget_bytes, side="right"))
        end = min(end, start + budget.max_chunk_rows, limit)
        if end <= start:
            end = start + 1  # a single row over the budget, within max_row_bytes
        length = end - start
        owned = [own_array(batch.column(j).slice(start, length)) for j in range(batch.num_columns)]
        yield pa.Table.from_arrays(owned, names=batch.schema.names)
        del owned
        start = end
    if limit < rows:
        raise RowExceedsMaxRowBytes(table=table, row=row_base + limit)


def iter_parquet_chunks(path: str, budget: ChunkBudget, *, table: str) -> Iterator[pa.Table]:
    """Owned chunks of a Parquet file within `budget`, row group by row group."""
    schema = pq.read_schema(path)
    dictionary_columns = [f.name for f in schema if str(f.type) in _STRING_TYPES]
    parquet_file = pq.ParquetFile(path, read_dictionary=dictionary_columns or None)
    row_base = 0
    for group in range(parquet_file.num_row_groups):
        for batch in parquet_file.iter_batches(batch_size=budget.read_rows, row_groups=[group], use_threads=False):
            rows = batch.num_rows
            yield from cut_batch(batch, budget, table=table, row_base=row_base)
            row_base += rows
            del batch


def csv_convert_options(names: list[str]) -> pa_csv.ConvertOptions:
    return pa_csv.ConvertOptions(
        column_types={name: pa.string() for name in names},
        null_values=list(PANDAS_DEFAULT_NA_VALUES),
        strings_can_be_null=True,
    )


def iter_csv_chunks(path: str, budget: ChunkBudget, *, table: str) -> Iterator[pa.Table]:
    """Owned string-typed chunks of a CSV file within `budget`, framed record by
    record so no record larger than `max_row_bytes` ever reaches the parser."""
    parse_options = pa_csv.ParseOptions(newlines_in_values=True)
    with open(path, "rb") as fh:
        records = framer.iter_csv_records(fh, max_record_bytes=budget.max_row_bytes)
        record_index = 0
        try:
            header = next(records, None)
        except framer.CsvRecordTooLarge:
            raise CsvRecordExceedsMaxRowBytes(table=table, record=0) from None
        except framer.CsvUnterminatedQuote:
            raise CsvMalformed(table=table, record=0) from None
        if header is None:
            return
        names = framer.split_record_fields(header)
        if names is None:
            raise CsvMalformed(table=table, record=0)
        convert_options = csv_convert_options(names)
        ncols = len(names)
        per_record = 4 * ncols + math.ceil(ncols / 8) + budget.out_row_bound
        batch: list[bytes] = []
        batch_estimate = 0
        first_index = 1

        def flush() -> pa.Table:
            payload = header + b"".join(batch)
            # One block holds the whole framed batch: PyArrow refuses a record that
            # straddles blocks, and the framer already bounded every record.
            read_options = pa_csv.ReadOptions(use_threads=False, block_size=min(len(payload) + 1, 2**31 - 1))
            try:
                return pa_csv.read_csv(
                    io.BytesIO(payload),
                    read_options=read_options,
                    parse_options=parse_options,
                    convert_options=convert_options,
                )
            except pa.ArrowInvalid:
                raise CsvMalformed(table=table, record=first_index) from None

        while True:
            try:
                record = next(records, None)
            except framer.CsvRecordTooLarge:
                raise CsvRecordExceedsMaxRowBytes(table=table, record=record_index + 1) from None
            except framer.CsvUnterminatedQuote:
                raise CsvMalformed(table=table, record=record_index + 1) from None
            if record is None:
                break
            record_index += 1
            estimate = len(record) + per_record
            if batch and (len(batch) + 1 > budget.max_chunk_rows or batch_estimate + estimate > budget.chunk_budget_bytes):
                yield flush()
                batch, batch_estimate = [], 0
                first_index = record_index
            batch.append(record)
            batch_estimate += estimate
        if batch:
            yield flush()


__all__ = [
    "PANDAS_DEFAULT_NA_VALUES",
    "BoundedReadError",
    "ChunkBudget",
    "CsvMalformed",
    "CsvRecordExceedsMaxRowBytes",
    "RowExceedsMaxRowBytes",
    "csv_convert_options",
    "cut_batch",
    "iter_csv_chunks",
    "iter_parquet_chunks",
    "own_array",
]
