"""F29 A7 (stored basis): the stored-output byte count is the size of the real closed file.

The reference sink reports `bytes_done` on the `stored_output` basis after a writer closed, for
both of its write paths. Rate samples keep the Arrow-logical basis; the two are never mixed.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import progress
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink


def _publish_events(seen: list[progress.ProgressEvent]) -> list[progress.PhaseEvent]:
    return [e for e in seen if isinstance(e, progress.PhaseEvent) and e.name == "publish"]


def test_write_reports_the_serialized_size_of_the_closed_file(tmp_path: Path) -> None:
    seen: list[progress.ProgressEvent] = []
    sink = ParquetTransactionalSink(tmp_path / "out")
    table = pa.table({"a": list(range(1000)), "b": [f"v{i}" for i in range(1000)]})
    with progress.use_progress(progress.CallbackSink(seen.append)):
        sink.write("t", table)
        sink.commit()
    start, finish = _publish_events(seen)
    assert (start.status, finish.status, finish.table) == ("start", "finish", "t")
    real = (tmp_path / "out" / "t.parquet").stat().st_size
    assert finish.bytes_done == real == sink.publication_evidence.stored_bytes["t"]
    assert finish.byte_basis == "stored_output"
    assert pq.read_table(tmp_path / "out" / "t.parquet").num_rows == 1000


def test_write_batches_reports_after_the_footer_is_closed(tmp_path: Path) -> None:
    seen: list[progress.ProgressEvent] = []
    sink = ParquetTransactionalSink(tmp_path / "out")
    schema = pa.schema([("a", pa.int64())])
    batches = [
        pa.record_batch([pa.array(range(i * 100, i * 100 + 100))], schema=schema) for i in range(5)
    ]
    with progress.use_progress(progress.CallbackSink(seen.append)) as channel:
        sink.write_batches("t", channel.count_rows("t", batches), schema=schema)
        sink.commit()
    finish = next(e for e in _publish_events(seen) if e.status == "finish")
    assert finish.bytes_done == (tmp_path / "out" / "t.parquet").stat().st_size
    rates = [e for e in seen if isinstance(e, progress.RateSample)]
    assert rates and {r.byte_basis for r in rates} == {"arrow_logical"}
    assert rates[-1].rows_done == 500


def test_no_sink_bound_means_no_events_and_the_same_file(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    sink.write("t", pa.table({"a": [1, 2, 3]}))
    sink.commit()
    assert (tmp_path / "out" / "t.parquet").is_file()
