"""CSV `ProfileSource` reader and its row-count estimator.

Split out of `profile/_readers.py` to keep that module under the size goal.
`_readers` re-exports `CsvFileSource` and `estimate_csv_rows`, so import them
from there; this module only holds the CSV cluster. The read itself (dialect, headerless
width scan, lexeme converters) is `_csv_reader._read_csv_rows`, shared with `read_csv_source`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pandas as pd
import pyarrow as pa

from decoy_engine import fileaccess
from decoy_engine.config._csv_dialect import CsvDialect
from decoy_engine.fileaccess import FileAccess
from decoy_engine.profile._csv_reader import _read_csv_rows
from decoy_engine.profile._policy_bound import _PolicyBound

if TYPE_CHECKING:
    from decoy_engine.profile._readers import RowCount

# How many lines to sample when estimating a CSV's mean row width. Bounded so
# the estimate stays cheap even on a large object (local read, or one ranged
# fetch on cloud). Small samples are fine: the estimate is coarse by design.
_CSV_ESTIMATE_LINES = 1_000


@dataclass(frozen=True)
class CsvFileSource(_PolicyBound):
    """`ProfileSource` for a local CSV file.

    CSV has no footer, so the row count is an O(1) byte-size estimate:
    `(filesize - header_bytes) // mean_row_bytes`, with mean row width measured
    from a bounded header sample. The estimate is flagged (`exact=False`). The
    bounded sample is `pd.read_csv(nrows=...)`.

    `has_header` / `column_names` / `delimiter` mirror the `FileSource` fields
    (same contract) so a headerless or non-comma file profiles with the names
    its mask rules key on, not pandas' positional `0,1,2...`.
    """

    path: Path
    has_header: bool = True
    column_names: list[str] | None = None
    delimiter: str = ","
    lexeme_columns: tuple[str, ...] = ()
    access: FileAccess | None = field(default=None, compare=False, repr=False)
    field_key: str = field(default="source.path", compare=False)
    table: str = field(default="source", compare=False)

    def __post_init__(self) -> None:
        if self.access is None:
            object.__setattr__(self, "access", fileaccess.capture())

    def _read(self, nrows: int | None = None) -> pd.DataFrame:
        dialect = CsvDialect(
            self.has_header,
            tuple(self.column_names) if self.column_names is not None else None,
            self.delimiter,
        )
        return cast(
            pd.DataFrame,
            _read_csv_rows(
                self._handle,
                dialect,
                table=self.table,
                nrows=nrows,
                lexeme_columns=self.lexeme_columns,
            ),
        )

    def row_count(self) -> RowCount:
        header_bytes = 0
        line_bytes: list[int] = []
        with self._handle() as handle:
            total = handle.stat().st_size
            with handle.open_binary() as fh:
                if self.has_header:
                    # A header row is not a data row; its bytes are excluded from
                    # the per-row mean. A headerless file has no such line to skip.
                    header_bytes = len(fh.readline())
                for _ in range(_CSV_ESTIMATE_LINES):
                    line = fh.readline()
                    if not line:
                        break
                    line_bytes.append(len(line))
        return estimate_csv_rows(total, header_bytes, line_bytes)

    def schema(self) -> pa.Schema:
        head = self._read(nrows=_CSV_ESTIMATE_LINES)
        return pa.Table.from_pandas(head, preserve_index=False).schema

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        return self._read(nrows=sample_rows)

    def to_frame(self) -> pd.DataFrame:
        return self._read()


def estimate_csv_rows(total_bytes: int, header_bytes: int, line_bytes: list[int]) -> RowCount:
    """Estimate CSV data rows from file size and a bounded line-width sample.

    Divides the post-header byte budget by the mean sampled line width. The
    result is clamped to at least the number of sampled lines so a downstream
    bounded sample can never exceed the reported row count (the ColumnProfile
    `distinct_count <= row_count` invariant). Always flagged `exact=False`.
    """
    # Deferred: _readers imports this module, so a top-level import would cycle.
    from decoy_engine.profile._readers import RowCount

    sampled = len(line_bytes)
    if sampled == 0:
        # Header-only or empty file: zero data rows (estimate, but confidently so).
        return RowCount(0, exact=False)
    mean_row_bytes = sum(line_bytes) / sampled
    data_bytes = max(total_bytes - header_bytes, 0)
    estimate = int(data_bytes / mean_row_bytes) if mean_row_bytes > 0 else sampled
    return RowCount(max(estimate, sampled), exact=False)
