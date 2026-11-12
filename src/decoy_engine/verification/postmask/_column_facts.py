"""Exact per-column facts for the post-scan column suite, computed over batches.

Every number is a count (or a ratio of two counts), so it does not depend on where a batch
starts or ends. Detector rates sum hits and non-null values over the batches before the
threshold test; averaging per-batch rates would not equal the whole-column rate.

Source and output are read in lockstep and re-cut to equal lengths, because the two readers
need not share batch boundaries. When the row counts differ the positional comparison is
skipped, as `residual_pii` and `policy_validation` do. Holds counts, names and detector ids only.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa

from decoy_engine.detection.detectors import _confidence_bucket, counting_scope, run_all_detectors
from decoy_engine.profiling import KEY_BATCH_ROWS, column_distinct
from decoy_engine.verification.postmask._stream_input import TableBatches


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

    @property
    def source_constant(self) -> bool:
        return self.source_distinct <= 1


@dataclass
class _Tally:
    comparable: int = 0
    identical: int = 0
    same_bytes: bool = True
    # detector id -> [hits, non-null values, threshold, name hint]
    detectors: dict[str, list[float | bool]] | None = None


def _series(batch: pa.RecordBatch, column: str) -> pd.Series:
    # Python ints for integers with nulls, so a null in one batch cannot retype the column.
    return batch.column(batch.schema.get_field_index(column)).to_pandas(integer_object_nulls=True)


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
    tally.same_bytes = tally.same_bytes and bool(src_o.equals(out_o))


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
        rate = int(hits) / int(n) if n else 0.0
        if n and rate >= float(threshold):
            rates.append(
                DetectorRate(
                    detector_id, round(rate, 4), _confidence_bucket(rate, name_hint=bool(name_hint))
                )
            )
    return tuple(sorted(rates, key=lambda r: r.match_rate, reverse=True))


def _distinct(table: TableBatches, column: str, batch_rows: int, scratch: Path, limit: str) -> int:
    schema = pa.schema([table.schema.field(column)])
    reader = pa.RecordBatchReader.from_batches(schema, table.read([column], batch_rows))
    return column_distinct(reader, scratch=scratch, memory_limit=limit)


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
    if aligned and source is not None and compared:
        pairs = _aligned(source.read(compared, batch_rows), output.read(wanted, batch_rows))
    else:
        compared = []
        pairs = ((None, b) for b in output.read(wanted, batch_rows))
    for src_batch, out_batch in pairs:
        for c in wanted:
            out_s = _series(out_batch, c)
            if src_batch is not None and c in compared:
                _add_identity(tallies[c], _series(src_batch, c), out_s)
            _add_detectors(tallies[c], out_s, c)
    facts = {}
    for c in wanted:
        t = tallies[c]
        has_source = source is not None and c in source.schema.names
        facts[c] = ColumnFacts(
            table=output.name,
            column=c,
            source_rows=source.num_rows if source is not None else 0,
            output_rows=output.num_rows,
            comparable_rows=t.comparable,
            identical_rows=t.identical,
            bytes_identical=aligned and c in compared and t.same_bytes,
            source_distinct=(
                _distinct(source, c, batch_rows, scratch, memory_limit)
                if source is not None and has_source
                else 0
            ),
            output_distinct=_distinct(output, c, batch_rows, scratch, memory_limit),
            detectors=_fired(t),
        )
    return facts
