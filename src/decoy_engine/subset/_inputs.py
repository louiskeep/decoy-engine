"""Table inputs: the one seam through which `subset/` reads a table's key columns.

Three physical kinds sit behind `TableInput`: a resident Arrow table, a lazy batch source
(Parquet or CSV) and a Parquet path. Types are the real Arrow types; nothing here casts a key,
so a text CSV key against an integer Parquet key is refused by preflight, not coerced. These
classes hold the only path-based Parquet reads in the package (`tests/sentry/test_subset_inputs.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import polars as pl
import pyarrow as pa

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileHandle, FileRequest, FileRole
from decoy_engine.fileaccess.adapters import polars as polars_adapter
from decoy_engine.profile import LazySource
from decoy_engine.subset._keys import RI
from decoy_engine.subset._types import SubsetSource

_BATCH_ROWS = 65_536


class TableInput(Protocol):
    name: str

    def polars_schema(self) -> pl.Schema: ...

    def num_rows(self) -> int: ...

    def key_frame(self, columns: Sequence[str]) -> pl.DataFrame:
        """The `RI` row-index column plus `columns`, in the order given. No columns: an
        `RI`-only frame of `num_rows()` rows."""
        ...


def _index_only(rows: int) -> pl.DataFrame:
    return pl.DataFrame({RI: pl.int_range(rows, dtype=pl.UInt32, eager=True)})


def _with_index(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_row_index(RI)


class ResidentInput:
    def __init__(self, name: str, table: pa.Table) -> None:
        self.name = name
        self._table = table

    def polars_schema(self) -> pl.Schema:
        return pl.from_arrow(self._table.slice(0, 0)).schema  # type: ignore[union-attr]

    def num_rows(self) -> int:
        return self._table.num_rows

    def key_frame(self, columns: Sequence[str]) -> pl.DataFrame:
        if not columns:
            return _index_only(self.num_rows())
        return _with_index(pl.from_arrow(self._table.select(list(columns))))  # type: ignore[arg-type]


class LazyInput:
    """Reads through the policy the source captured when it was built, never `current()`."""

    def __init__(self, name: str, source: LazySource, batch_rows: int = _BATCH_ROWS) -> None:
        self.name = name
        self._source = source
        self._batch_rows = batch_rows

    def polars_schema(self) -> pl.Schema:
        return pl.from_arrow(self._source.schema.empty_table()).schema  # type: ignore[union-attr]

    def num_rows(self) -> int:
        return self._source.num_rows

    def key_frame(self, columns: Sequence[str]) -> pl.DataFrame:
        if not columns:
            return _index_only(self.num_rows())
        cols = list(columns)
        batches = list(self._source.iter_batches(self._batch_rows, columns=cols))
        schema = pa.schema([self._source.schema.field(c) for c in cols])
        table = pa.Table.from_batches(batches, schema=schema).select(cols)
        return _with_index(pl.from_arrow(table))  # type: ignore[arg-type]


class ParquetPathInput:
    """A Parquet file opened under the installed policy. The config position is
    `subset.sources.<table>.path`, never the path itself."""

    def __init__(self, name: str, source: SubsetSource) -> None:
        self.name = name
        self.source = source

    def _open(self) -> FileHandle:
        return fileaccess.current().open_read(
            FileRequest(
                FileRole.SUBSET_INPUT, "read", self.source.path, f"subset.sources.{self.name}.path"
            )
        )

    def polars_schema(self) -> pl.Schema:
        with self._open() as handle:
            return polars_adapter.collect_schema(handle, polars_adapter.scan_parquet(handle))

    def num_rows(self) -> int:
        with self._open() as handle:
            counted = polars_adapter.collect(
                handle, polars_adapter.scan_parquet(handle).select(pl.len())
            )
        return int(counted.item())

    def key_frame(self, columns: Sequence[str]) -> pl.DataFrame:
        with self._open() as handle:
            lazy = polars_adapter.scan_parquet(handle).with_row_index(RI)
            return polars_adapter.collect(handle, lazy.select([RI, *columns]))

    def filtered_frame(self, indices: Sequence[int]) -> pl.DataFrame:
        """The rows at `indices`, in file order, without the index column."""
        with self._open() as handle:
            lazy = polars_adapter.scan_parquet(handle).with_row_index(RI)
            return polars_adapter.collect(
                handle, lazy.filter(pl.col(RI).is_in(list(indices))).drop(RI)
            )


def as_input(name: str, value: object) -> TableInput:
    """Wrap one supplied table. Anything but the three known kinds is a programming error."""
    if isinstance(value, pa.Table):
        return ResidentInput(name, value)
    if isinstance(value, LazySource):
        return LazyInput(name, value)
    if isinstance(value, SubsetSource):
        return ParquetPathInput(name, value)
    raise TypeError(f"table {name!r}: unsupported subset input type {type(value).__name__}")
