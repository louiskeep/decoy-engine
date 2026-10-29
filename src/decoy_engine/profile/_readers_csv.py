"""CSV `ProfileSource` reader and its row-count estimator.

Split out of `profile/_readers.py` to keep that module under the size goal.
`_readers` re-exports `CsvFileSource` and `estimate_csv_rows`, so import them
from there; this module only holds the CSV cluster.
"""

from __future__ import annotations

import csv
import io
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa

from decoy_engine import fileaccess
from decoy_engine.errors import PipelineValidationError
from decoy_engine.fileaccess import FileAccess
from decoy_engine.fileaccess.adapters import pandas as pandas_adapter
from decoy_engine.profile._policy_bound import PolicyBound
from decoy_engine.validation_result import CODES

# The headerless width scan (see CsvFileSource._assert_record_widths) tokenizes
# with the stdlib csv parser, which defaults to a 131072-char field cap pandas
# does not have. Raise it to the platform maximum so a large text/JSON field is
# not falsely rejected. Narrow back on platforms where sys.maxsize overflows the
# C long the module uses.
_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(_limit)
        break
    except OverflowError:
        _limit //= 2
if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from decoy_engine.profile._readers import RowCount

# How many lines to sample when estimating a CSV's mean row width. Bounded so
# the estimate stays cheap even on a large object (local read, or one ranged
# fetch on cloud). Small samples are fine: the estimate is coarse by design.
_CSV_ESTIMATE_LINES = 1_000


@dataclass(frozen=True)
class CsvFileSource(PolicyBound):
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
    access: FileAccess | None = field(default=None, compare=False, repr=False)
    field_key: str = field(default="source.path", compare=False)

    def __post_init__(self) -> None:
        if self.access is None:
            object.__setattr__(self, "access", fileaccess.capture())

    def _read(self, nrows: int | None = None) -> pd.DataFrame:
        if self.has_header:
            with self._handle() as handle:
                return pandas_adapter.read_csv(handle, nrows=nrows, sep=self.delimiter)
        # Headerless: pandas cannot fail closed on a mis-sized file. A too-short
        # row is silently padded (and earlier values shift into the wrong column);
        # a too-long row is dropped or shifts. Frame-width alone does not catch a
        # ragged row, because pandas normalizes every row to the widest one. So
        # validate the field count of each consumed record with the stdlib csv
        # parser (which honours quoting and multiline-quoted fields the same way
        # pandas does for a single-char delimiter), then read positionally and
        # assign the names. Messages name only the path, line and counts -- never a
        # cell value, since the file may itself hold the PII this product masks.
        names = self.column_names or []
        self._assert_record_widths(len(names), nrows)
        try:
            with self._handle() as handle:
                frame = pandas_adapter.read_csv(
                    handle, nrows=nrows, sep=self.delimiter, header=None
                )
        except (pd.errors.EmptyDataError, pd.errors.ParserError):
            # The width scan above already rejects the known mis-size cases with a
            # coded error; this is a fail-closed backstop for any parser disagreement.
            # (Under a strict policy the adapter resurfaces a read failure as a
            # FileAccessReadError before this; under the ambient policy the library
            # error passes through and is caught here, as before the policy port.)
            raise PipelineValidationError(
                f"headerless file {self.path.name!r} could not be read against its "
                f"{len(names)} declared column_names",
                code=CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
            ) from None
        if frame.shape[1] != len(names):
            # The csv-module width scan and pandas tokenize independently, so a
            # crafted byte sequence can make them disagree on field count (e.g. a
            # NUL before a quote: the scan drops the NUL and sees a quoted field,
            # pandas keeps it and splits on the inner delimiter). A raw
            # `frame.columns = names` would then raise an uncoded ValueError; fail
            # closed with the coded error instead. Counts only, never a cell value.
            raise PipelineValidationError(
                f"headerless file {self.path.name!r} parsed to {frame.shape[1]} columns "
                f"against its {len(names)} declared column_names",
                code=CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
            )
        frame.columns = names
        return frame

    def _assert_record_widths(self, expected: int, nrows: int | None) -> None:
        """Fail closed when a consumed record's field count != `expected`.

        Scans only the records the read will consume (`nrows`), so a huge file is
        not walked in full for a bounded sample, and raises a coded
        PipelineValidationError naming the physical line and the two counts, never
        a cell value. The scan reads through the policy-issued file handle (so a
        strict policy still vouches for the file), configured to agree with the
        pandas reader it guards: `utf-8-sig` strips the BOM pandas strips; the
        field-size cap is lifted (module top); and a line is skipped only when it is
        empty or a single whitespace-only field with no delimiter, which is exactly
        what pandas' skip_blank_lines does (a whitespace line that contains the
        delimiter, or an empty-field line like ",", is a real row to both).

        Two byte classes are rejected outright (coded error) rather than sanitized,
        because the csv parser and pandas tokenize them differently so no sanitize
        step can keep the two in agreement:
          - a NUL byte (not valid in a text delimited file; pandas keeps it, so a
            NUL before a quote makes the quote literal, while the csv parser rejects
            it);
          - a lone CR -- a carriage return not part of a CRLF pair (an old-Mac line
            ending, or a CR inside a record): the two readers treat it as a row
            boundary differently, which can shift a value into the wrong column
            without changing the frame width.
        Only LF and CRLF line endings are reconcilable, so failing closed on NUL and
        lone CR is what keeps the two readers agreeing on LINE BOUNDARIES for every
        file this scan admits. They can still disagree WITHIN a line -- an
        unterminated quote (`1,2,"3`), for one, which the stdlib csv parser tolerates
        but pandas rejects -- and that residual disagreement is caught by the
        pandas read below: it raises `EmptyDataError`/`ParserError`, or the
        `frame.shape[1]` backstop fires, both coded, so no frame or misaligned value
        escapes.

        Blank-line limitation (now bounded): a line whose sole field is quoted-empty
        (`""`), quoted-whitespace (`" "`, `"\t"`), or a bare/quoted non-breaking
        space is treated as blank by this scan and not width-checked, while pandas
        retains it as a one-field row. Because NUL and lone CR are rejected above,
        the csv scan and pandas agree on line boundaries for every admitted file, so a
        skipped line is genuinely blank to pandas too: pandas pads it to a
        blank-equivalent row whose only non-NaN value is that single whitespace
        field in its correct leading column, with the rest NaN. The skip predicate
        only fires for a <=1-field record whose sole field strips to empty, so a
        line carrying real (non-whitespace) field-aligned data is always
        width-checked. No real value is ever misaligned and no cell leaks; the only
        thing that slips is a leading whitespace-only cell in its own correct
        column."""
        consumed = 0
        with self._handle() as source, source.open_binary() as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")

            def _reject_unreconcilable(lines: Iterable[str]) -> Iterator[str]:
                # NUL and a lone CR are not reconcilable between the csv scan and
                # pandas (see the docstring); fail closed before tokenizing. Path +
                # reason only, never a cell value. With newline="" the handle splits
                # on CR, LF and CRLF, so a lone-CR ending leaves a trailing "\r" and
                # a CRLF ending a trailing "\r\n"; strip one trailing LF/CRLF, then
                # any remaining "\r" is a lone CR.
                for line in lines:
                    if "\x00" in line:
                        raise PipelineValidationError(
                            f"headerless file {self.path.name!r} contains a NUL byte, "
                            "which is not valid in a text delimited file",
                            code=CODES.SOURCE_FILE_NUL_BYTE,
                        )
                    body = line[:-2] if line.endswith("\r\n") else line.rstrip("\n")
                    if "\r" in body:
                        raise PipelineValidationError(
                            f"headerless file {self.path.name!r} contains a lone "
                            "carriage return (only LF and CRLF line endings are supported)",
                            code=CODES.SOURCE_FILE_LONE_CR,
                        )
                    yield line

            reader = csv.reader(_reject_unreconcilable(text), delimiter=self.delimiter)
            for record in reader:
                if len(record) <= 1 and (not record or not record[0].strip()):
                    continue
                if len(record) != expected:
                    raise PipelineValidationError(
                        f"headerless file {self.path.name!r} line {reader.line_num} has "
                        f"{len(record)} fields but column_names declares {expected}",
                        code=CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
                    )
                consumed += 1
                if nrows is not None and consumed >= nrows:
                    return
        if consumed == 0:
            raise PipelineValidationError(
                f"headerless file {self.path.name!r} has no data rows to read "
                f"against its {expected} declared column_names",
                code=CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
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
