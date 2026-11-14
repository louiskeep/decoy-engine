"""`ParquetTransactionalSink.staged_tables()` and `from_staged`: the read-back seam of the gate."""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import post_scan_output_readable, post_scan_sink_kind
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.verification.postmask import from_staged


def _read(opener) -> pa.Table:  # type: ignore[no-untyped-def]
    with os.fdopen(opener(), "rb") as fh:
        return pq.read_table(fh)


def test_staged_tables_names_every_written_table(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    assert sink.staged_tables() == {}
    sink.write("a", pa.table({"x": [1, 2]}))
    sink.write_batches("b", [pa.record_batch({"y": ["p"]})], schema=pa.schema([("y", pa.string())]))
    staged = sink.staged_tables()
    assert set(staged) == {"a", "b"}
    assert _read(staged["a"]).column("x").to_pylist() == [1, 2]
    assert _read(staged["b"]).column("y").to_pylist() == ["p"]
    assert staged["a"]() != staged["a"]()  # a new descriptor each call
    for opener in staged.values():
        os.close(opener())


def test_writing_a_table_again_keeps_one_entry(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    sink.write("a", pa.table({"x": [1]}))
    sink.write("a", pa.table({"x": [7, 8]}))
    assert list(sink.staged_tables()) == ["a"]
    assert _read(sink.staged_tables()["a"]).num_rows == 2


def test_nothing_is_staged_after_commit_or_abort(tmp_path: Path) -> None:
    committed = ParquetTransactionalSink(tmp_path / "c")
    committed.write("a", pa.table({"x": [1]}))
    committed.commit()
    assert committed.staged_tables() == {}
    aborted = ParquetTransactionalSink(tmp_path / "d")
    aborted.write("a", pa.table({"x": [1]}))
    aborted.abort()
    assert aborted.staged_tables() == {}


def test_from_staged_takes_schema_and_rows_from_the_footer_and_closes_every_descriptor(
    tmp_path: Path,
) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    sink.write("a", pa.table({"x": list(range(10)), "y": [str(i) for i in range(10)]}))
    opened: list[int] = []
    real = sink.staged_tables()["a"]

    def opener() -> int:
        fd = real()
        opened.append(fd)
        return fd

    table = from_staged("a", opener)
    assert table.num_rows == 10 and table.schema.names == ["x", "y"]
    got = pa.Table.from_batches(list(table.read(["y"], 4)))
    assert got.column("y").to_pylist() == [str(i) for i in range(10)]
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_the_readable_sink_kinds() -> None:
    sink = ParquetTransactionalSink.__new__(ParquetTransactionalSink)
    assert post_scan_output_readable(post_scan_sink_kind(None))
    assert post_scan_output_readable(post_scan_sink_kind(sink))
    assert not post_scan_output_readable(post_scan_sink_kind(object()))
