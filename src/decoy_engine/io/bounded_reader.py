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

from decoy_engine import fileaccess
from decoy_engine.errors import DecoyError
from decoy_engine.fileaccess import FileAccess, FileRequest, FileRole
from decoy_engine.fileaccess.adapters import arrow as arrow_adapter
from decoy_engine.io import csv_framer as framer

# pandas' default `na_values`, the set the full-frame CSV read (`read_csv(dtype=str)`)
# treats as null; the chunked read must match it for CSV->CSV byte parity.
PANDAS_DEFAULT_NA_VALUES: tuple[str, ...] = (
    "",
    "#N/A",
    "#N/A N/A",
    "#NA",
    "-1.#IND",
    "-1.#QNAN",
    "-NaN",
    "-nan",
    "1.#IND",
    "1.#QNAN",
    "<NA>",
    "N/A",
    "NA",
    "NULL",
    "NaN",
    "None",
    "n/a",
    "nan",
    "null",
)

# Offset-buffer widths. A 32-bit-offset value (string/binary) carries a 4-byte offset;
# a 64-bit-offset value (large_string/large_binary) carries an 8-byte one. An offset
# buffer for N values holds N+1 offsets, so the terminal (N+1th) offset is reserved once
# per column per chunk on top of the per-value offset.
_OFFSET_WIDTH_32 = 4
_OFFSET_WIDTH_64 = 8
# One validity byte per value, conservative against Arrow's bit-packed validity (ceil(N/8))
# so the budget is never under-counted; also the per-row charge for a fixed-width value.
_FIXED_OVERHEAD = 1
# 32-bit vs 64-bit offset string/binary types, split because they are priced differently.
_STRING_TYPES_32 = ("string", "binary")
_STRING_TYPES_64 = ("large_string", "large_binary")
# `iter_parquet_chunks` reads these as dictionaries; both offset widths qualify.
_STRING_TYPES = _STRING_TYPES_32 + _STRING_TYPES_64
_pc: Any = pc


class BoundedReadError(DecoyError):
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


class UnsupportedColumnType(BoundedReadError):  # noqa: N818 - coded condition
    code = "unsupported_column_type"


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
        if (
            isinstance(self.out_row_bound, bool)
            or not isinstance(self.out_row_bound, int)
            or self.out_row_bound < 0
        ):
            raise ValueError(
                f"ChunkBudget.out_row_bound must be a non-negative int; got {self.out_row_bound!r}"
            )


def _binary_length(array: pa.Array) -> pa.Array:
    return _pc.binary_length(array)


def own_array(array: pa.Array) -> pa.Array:
    """A copy of ``array`` in newly allocated buffers. A dictionary array is
    decoded to its value type."""
    if isinstance(array, pa.DictionaryArray):
        return array.dictionary_decode()
    return pa.concat_arrays([array])


def _offset_width(arrow_type: pa.DataType) -> int | None:
    """Offset width in bytes for a variable-width string/binary type (4 for 32-bit
    string/binary, 8 for large_string/large_binary), or None if `arrow_type` is not one.
    large_* carry 8-byte offsets, so under-counting them is one way the bound broke."""
    name = str(arrow_type)
    if name in _STRING_TYPES_32:
        return _OFFSET_WIDTH_32
    if name in _STRING_TYPES_64:
        return _OFFSET_WIDTH_64
    return None


def _physical_spans(column: pa.Array, width: int) -> np.ndarray:
    """Per-row PHYSICAL payload bytes `offsets[i+1] - offsets[i]`, read straight off the
    offset buffer rather than from the logical (null-aware) value length. Arrow permits a
    null slot to span a nonzero offset range, and `own_array` (concat) preserves that
    payload in the yielded chunk, so pricing a null slot at zero under-counts the buffer."""
    dtype = np.int32 if width == _OFFSET_WIDTH_32 else np.int64
    raw = np.frombuffer(column.buffers()[1], dtype=dtype)
    base = column.offset  # a sliced array shares the parent offset buffer
    offsets = raw[base : base + len(column) + 1].astype(np.int64)
    return offsets[1:] - offsets[:-1]


def _column_costs(column: pa.Array, *, table: str) -> tuple[np.ndarray, int]:
    """Conservative (over-estimating, never under) PHYSICAL decoded bytes for one column:
    a per-row cost array plus the terminal-offset reserve the chunk owes once for this
    column. Costs are read from the physical buffers, not a per-type formula, so null-slot
    payloads, the terminal offset, validity and padding are all covered.

    A type whose size is not a function of the schema -- a nested list/struct/map, or a
    view/other variable-width type this reader does not price explicitly -- is rejected
    (`UnsupportedColumnType`) rather than guessed, so a chunk can never exceed the caller's
    byte bound. `cut_batch` sums this across the batch BEFORE yielding any chunk, so the
    reject lands before a row is handed on."""
    if isinstance(column, pa.ExtensionArray):
        # An extension type WRAPS a storage array, and its `bit_width` delegates to the
        # storage (a dictionary storage reports its 32-bit index width), so the fixed-width
        # branch below would price the facade at the index width while the real payload is
        # the storage. Price the storage itself. `own_array` (concat) does NOT decode an
        # extension's storage the way it decodes a top-level DictionaryArray, so an
        # extension over dictionary storage retains the WHOLE dictionary in every chunk no
        # matter how few rows it holds -- unbounded per the schema -- and must be rejected
        # rather than priced by referenced-value lengths. For any other storage the physical
        # buffers compact with the slice exactly as the plain type does, so pricing the
        # storage recursively is conservative (and rejects nested/view storage in turn).
        if isinstance(column.storage, pa.DictionaryArray):
            raise UnsupportedColumnType(table=table, arrow_type=str(column.type))
        return _column_costs(column.storage, table=table)
    if isinstance(column, pa.DictionaryArray):
        # A dictionary decodes to its value type (`own_array`) in freshly built buffers, so
        # a null slot there spans no payload; the decoded value length is the physical span.
        # Price it plus the value type's per-value offset and validity (8-byte for large_*).
        width = _offset_width(column.type.value_type)
        if width is None:
            raise UnsupportedColumnType(table=table, arrow_type=str(column.type))
        lengths = pc.take(_binary_length(column.dictionary), column.indices)
        costs = pc.fill_null(lengths, 0).to_numpy(zero_copy_only=False).astype(np.int64)
        return costs + width + _FIXED_OVERHEAD, width
    width = _offset_width(column.type)
    if width is not None:
        return _physical_spans(column, width) + width + _FIXED_OVERHEAD, width
    if pa.types.is_nested(column.type):
        # A list/struct/map/union row's size is its payload, not its type, so it cannot be
        # priced per row from the schema alone; reject before the bound can be exceeded.
        raise UnsupportedColumnType(table=table, arrow_type=str(column.type))
    if pa.types.is_null(column.type):
        return np.full(len(column), _FIXED_OVERHEAD, dtype=np.int64), 0  # validity only
    try:
        byte_width = max(1, column.type.bit_width // 8)
    except ValueError:
        # A non-nested variable-width type we do not price explicitly (string_view,
        # binary_view, ...). Reject rather than guess a width and risk under-counting.
        raise UnsupportedColumnType(table=table, arrow_type=str(column.type)) from None
    return np.full(len(column), byte_width + _FIXED_OVERHEAD, dtype=np.int64), 0


def cut_batch(
    batch: pa.RecordBatch, budget: ChunkBudget, *, table: str, row_base: int = 0
) -> Iterator[pa.Table]:
    """Cut one decoded batch into owned chunks within `budget`; a row over
    `max_row_bytes` raises after the rows before it were yielded."""
    rows = batch.num_rows
    if rows == 0:
        return
    in_cost = np.zeros(rows, dtype=np.int64)
    terminal = 0  # the (N+1th) offset each produced chunk owns once per variable-width column
    for column in batch.columns:
        costs, reserve = _column_costs(column, table=table)
        in_cost += costs
        terminal += reserve
    # The terminal offset is a fixed per-chunk cost: charge it against every row's ceiling
    # and reserve it out of the chunk budget, so neither a single row nor a multi-row chunk
    # can exceed its bound once the chunk owns that extra offset entry.
    oversized = np.flatnonzero(in_cost + terminal > budget.max_row_bytes)
    limit = int(oversized[0]) if len(oversized) else rows
    effective_budget = budget.chunk_budget_bytes - terminal
    total = in_cost + budget.out_row_bound
    cumulative = np.cumsum(total)
    start = 0
    while start < limit:
        base = int(cumulative[start - 1]) if start else 0
        end = int(np.searchsorted(cumulative, base + effective_budget, side="right"))
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


@fileaccess.entry_point
def iter_parquet_chunks(
    path: str,
    budget: ChunkBudget,
    *,
    table: str,
    file_access: FileAccess | None = None,
    field_key: str = "source.path",
) -> Iterator[pa.Table]:
    """Owned chunks of a Parquet file within `budget`, row group by row group."""
    with fileaccess.current().open_read(
        FileRequest(FileRole.SOURCE, "read", path, field_key)
    ) as handle:
        schema = arrow_adapter.parquet_file(handle).schema_arrow
        dictionary_columns = [f.name for f in schema if str(f.type) in _STRING_TYPES]
        parquet_file = arrow_adapter.parquet_file(
            handle, read_dictionary=dictionary_columns or None
        )
        row_base = 0
        for group in range(parquet_file.num_row_groups):
            for batch in arrow_adapter.iter_batches(
                handle,
                parquet_file,
                batch_size=budget.read_rows,
                row_groups=[group],
                use_threads=False,
            ):
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


@fileaccess.entry_point
def iter_csv_chunks(
    path: str,
    budget: ChunkBudget,
    *,
    table: str,
    file_access: FileAccess | None = None,
    field_key: str = "source.path",
) -> Iterator[pa.Table]:
    """Owned string-typed chunks of a CSV file within `budget`, framed record by
    record so no record larger than `max_row_bytes` ever reaches the parser."""
    parse_options = pa_csv.ParseOptions(newlines_in_values=True)
    handle = fileaccess.current().open_read(FileRequest(FileRole.SOURCE, "read", path, field_key))
    with handle, handle.open_binary() as fh:
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
            read_options = pa_csv.ReadOptions(
                use_threads=False, block_size=min(len(payload) + 1, 2**31 - 1)
            )
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
            if batch and (
                len(batch) + 1 > budget.max_chunk_rows
                or batch_estimate + estimate > budget.chunk_budget_bytes
            ):
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
    "UnsupportedColumnType",
    "csv_convert_options",
    "cut_batch",
    "iter_csv_chunks",
    "iter_parquet_chunks",
    "own_array",
]
