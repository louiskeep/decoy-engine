"""Avro and ORC `ProfileSource`s, beside `_readers_csv.py`.

Both read through `decoy_engine.io` (the same readers the chunked and whole-file routes use),
under the whole-file admission: a nested or single-block Avro file still profiles, because
`run_pipeline` and the capacity and job estimates profile every source. Row counts are exact
claims of the file (the ORC tail; the Avro block headers, summed without decoding), not
verified against decoded rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pyarrow as pa

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccess
from decoy_engine.io import avro_io, orc_io
from decoy_engine.profile._policy_bound import _PolicyBound
from decoy_engine.profile._readers import RowCount


@dataclass(frozen=True)
class AvroFileSource(_PolicyBound):
    """`ProfileSource` for a local Avro object container file."""

    path: Path
    access: FileAccess | None = field(default=None, compare=False, repr=False)
    field_key: str = field(default="source.path", compare=False)
    table: str = field(default="source", compare=False)

    def __post_init__(self) -> None:
        if self.access is None:
            object.__setattr__(self, "access", fileaccess.capture())

    def row_count(self) -> RowCount:
        with self._handle() as handle, handle.open_binary() as stream:
            return RowCount(avro_io.avro_row_count(handle, stream, table=self.table), exact=True)

    def schema(self) -> pa.Schema:
        with self._handle() as handle, handle.open_binary() as stream:
            return avro_io.avro_schema_of(handle, stream, table=self.table)

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        with self._handle() as handle, handle.open_binary() as stream:
            return avro_io.avro_head(handle, stream, sample_rows, table=self.table).to_pandas()

    def to_frame(self) -> pd.DataFrame:
        with self._handle() as handle, handle.open_binary() as stream:
            return avro_io.read_avro_handle(handle, stream, table=self.table).to_pandas()


@dataclass(frozen=True)
class OrcFileSource(_PolicyBound):
    """`ProfileSource` for a local ORC file."""

    path: Path
    access: FileAccess | None = field(default=None, compare=False, repr=False)
    field_key: str = field(default="source.path", compare=False)
    table: str = field(default="source", compare=False)

    def __post_init__(self) -> None:
        if self.access is None:
            object.__setattr__(self, "access", fileaccess.capture())

    def row_count(self) -> RowCount:
        with self._handle() as handle:
            return RowCount(int(orc_io.open_orc(handle, table=self.table).nrows), exact=True)

    def schema(self) -> pa.Schema:
        with self._handle() as handle:
            return orc_io.open_orc(handle, table=self.table).schema

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        with self._handle() as handle:
            return orc_io.orc_head(handle, sample_rows, table=self.table).to_pandas()

    def to_frame(self) -> pd.DataFrame:
        with self._handle() as handle:
            return orc_io.read_orc_handle(handle, table=self.table).to_pandas()


__all__ = ["AvroFileSource", "OrcFileSource"]
