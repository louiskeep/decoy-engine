"""Exact per-column facts for the post-scan column suite, computed over batches.

Every number is a count (or a ratio of two counts), so it does not depend on where a batch
starts or ends. Detector rates sum hits and non-null values over the batches before the
threshold test; averaging per-batch rates would not equal the whole-column rate.

Source and output are read in lockstep and re-cut to equal lengths, because the two readers
need not share batch boundaries. When the row counts differ the positional comparison is
skipped, as `residual_pii` and `policy_validation` do. Holds counts, names and detector ids only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd
import pyarrow as pa

from decoy_engine.detection.detectors import counting_scope, decide_detector, run_all_detectors
from decoy_engine.internal.pandas_compat import exact_int_types_mapper
from decoy_engine.profiling import KEY_BATCH_ROWS, column_distinct, passes_through
from decoy_engine.verification.postmask._stream_input import TableBatches, key_batches


@dataclass(frozen=True)
class DetectorRate:
    """A detector that fires on the whole output column, as `run_all_detectors` reports it."""

    detector_id: str
    match_rate: float
    confidence: str


@dataclass(frozen=True)
class ColumnFacts:
    """Counts for one configured column. `comparable_rows` and `identical_rows` are 0 and
    `bytes_identical` is False when there is no source column or the row counts differ."""

    table: str
    column: str
    source_rows: int
    output_rows: int
    comparable_rows: int
    identical_rows: int
    bytes_identical: bool
    source_distinct: int
    output_distinct: int
    detectors: tuple[DetectorRate, ...]
    # Class names of uncoded failures on this one column: comparing source with output (identity
    # and bytes_identical are then unavailable), and counting distinct values (that count is then
    # 0). The detector rates are exact either way.
    identity_error: str | None = None
    distinct_error: str | None = None
    # True when the failed count was the source's, so a zero source_distinct means unknown.
    source_distinct_failed: bool = False

    @property
    def error(self) -> str | None:
        return self.identity_error or self.distinct_error

    @property
    def source_constant(self) -> bool:
        return self.source_distinct <= 1


@dataclass
class _Tally:
    comparable: int = 0
    identical: int = 0
    same_bytes: bool = True
    error: str | None = None
    # detector id -> [hits, non-null values, threshold, name hint]
    detectors: dict[str, list[float | bool]] | None = None


def _series(batch: pa.RecordBatch, column: str) -> pd.Series:
    # The dtype comes from the schema, so a null in one batch cannot retype an integer column.
    array = batch.column(batch.schema.get_field_index(column))
    return array.to_pandas(types_mapper=exact_int_types_mapper)


def _aligned(
    left: Iterator[pa.RecordBatch], right: Iterator[pa.RecordBatch]
) -> Iterator[tuple[pa.RecordBatch, pa.RecordBatch]]:
    """Pairs of batches of equal length covering both streams in order."""
    a = b = None
    while True:
        if a is None or a.num_rows == 0:
            a = next(left, None)
        if b is None or b.num_rows == 0:
            b = next(right, None)
        if a is None or b is None:
            # Exhaust both so the row-count check sees every row, not a prefix.
            for _ in left:
                pass
            for _ in right:
                pass
            return
        n = min(a.num_rows, b.num_rows)
        if n:
            yield a.slice(0, n), b.slice(0, n)
        a, b = a.slice(n), b.slice(n)


def _add_identity(tally: _Tally, src: pd.Series, out: pd.Series) -> None:
    src_o, out_o = src.astype(object), out.astype(object)
    present = src_o.notna() & out_o.notna()
    tally.comparable += int(present.sum())
    tally.identical += int((src_o[present] == out_o[present]).sum())
    # A null is NaN in a float column and pd.NA in an Arrow-typed integer one; `equals` treats
    # NaN and None as one null but not NaN and pd.NA, so a float source against an int output of
    # the same numbers would read as different.
    tally.same_bytes = tally.same_bytes and bool(
        src_o.where(src_o.notna(), None).equals(out_o.where(out_o.notna(), None))
    )


def _add_detectors(tally: _Tally, out: pd.Series, column: str) -> None:
    with counting_scope() as counts:
        run_all_detectors(out, column)
    seen = tally.detectors if tally.detectors is not None else {}
    for c in counts:
        row = seen.setdefault(c.detector_id, [0, 0, c.threshold, c.name_hint])
        row[0] = int(row[0]) + c.hits
        row[1] = int(row[1]) + c.n
    tally.detectors = seen


def _fired(tally: _Tally) -> tuple[DetectorRate, ...]:
    rates = []
    for detector_id, (hits, n, threshold, name_hint) in (tally.detectors or {}).items():
        decided = decide_detector(int(hits), int(n), float(threshold), bool(name_hint))
        if decided is not None:
            rates.append(DetectorRate(detector_id, round(decided[0], 4), decided[1]))
    return tuple(sorted(rates, key=lambda r: r.match_rate, reverse=True))


def _counted(table: TableBatches) -> tuple[TableBatches, Callable[[], None]]:
    """`table` with a row tally over its reads, and a check that the tally equals its declared
    row count. A stream that ends early or runs long would otherwise leave facts over a prefix."""
    seen = [0]

    def read(columns: Sequence[str], batch_rows: int) -> Iterator[pa.RecordBatch]:
        for batch in table.read(columns, batch_rows):
            seen[0] += batch.num_rows
            yield batch

    def verify() -> None:
        if seen[0] != table.num_rows:
            raise ValueError(
                f"column facts: a table read gave {seen[0]} rows but the table declares "
                f"{table.num_rows}"
            )

    return replace(table, read=read), verify


def _distinct_or_error(
    table: TableBatches, column: str, scratch: Path, limit: str
) -> tuple[int, str | None]:
    """The count and no error, or zero and the class name of an uncoded failure counting this one
    column. A short or long stream is not such a failure: `verify` refuses it for every column."""
    counted, verify = _counted(table)
    try:
        count = column_distinct(key_batches(counted, [column]), scratch=scratch, memory_limit=limit)
    except Exception as exc:
        if passes_through(exc):
            raise
        return 0, type(exc).__name__
    verify()
    return count, None


def column_facts(
    source: TableBatches | None,
    output: TableBatches,
    columns: Sequence[str],
    *,
    batch_rows: int = KEY_BATCH_ROWS,
    scratch: Path,
    memory_limit: str,
) -> dict[str, ColumnFacts]:
    """Facts for each of `columns` that the output has, keyed by column name."""
    wanted = [c for c in columns if c in output.schema.names]
    if not wanted:
        return {}
    compared = [c for c in wanted if source is not None and c in source.schema.names]
    aligned = source is not None and source.num_rows == output.num_rows
    tallies = {c: _Tally() for c in wanted}
    out_read, out_verify = _counted(output)
    checks = [out_verify]
    if aligned and source is not None and compared:
        src_read, src_verify = _counted(source)
        checks.append(src_verify)
        pairs = _aligned(src_read.read(compared, batch_rows), out_read.read(wanted, batch_rows))
    else:
        compared = []
        pairs = ((None, b) for b in out_read.read(wanted, batch_rows))
    for src_batch, out_batch in pairs:
        for c in wanted:
            out_s = _series(out_batch, c)
            if src_batch is not None and c in compared and tallies[c].error is None:
                try:
                    _add_identity(tallies[c], _series(src_batch, c), out_s)
                except Exception as exc:
                    # One column that cannot be compared (a list or map cell, say) must not
                    # take the other columns down with it.
                    if passes_through(exc):
                        raise
                    tallies[c].error = type(exc).__name__
            _add_detectors(tallies[c], out_s, c)
    for check in checks:
        check()
    facts = {}
    for c in wanted:
        t = tallies[c]
        has_source = source is not None and c in source.schema.names
        distinct_failed = None
        source_failed = False
        distinct = [0, 0]
        for n, (table, present) in enumerate(((source, has_source), (output, True))):
            if present and table is not None:
                distinct[n], err = _distinct_or_error(table, c, scratch, memory_limit)
                distinct_failed = distinct_failed or err
                source_failed = source_failed or (n == 0 and err is not None)
        facts[c] = ColumnFacts(
            table=output.name,
            column=c,
            source_rows=source.num_rows if source is not None else 0,
            output_rows=output.num_rows,
            comparable_rows=0 if t.error else t.comparable,
            identical_rows=0 if t.error else t.identical,
            bytes_identical=aligned and c in compared and t.same_bytes and not t.error,
            source_distinct=distinct[0],
            output_distinct=distinct[1],
            detectors=_fired(t),
            identity_error=t.error,
            distinct_error=distinct_failed,
            source_distinct_failed=source_failed,
        )
    return facts
