"""`test_sink_characterization`: the reference sink's publish behaviour, pinned before the sink
is split into a write half (`StagedParquetWriter`) and a state table (plan 5.6, 7.2; class 9).

Probes P1 to P6 (plan 1.3) on both POSIX policies, plus the write half's own contract: a table
written twice replaces its staged file, names are single components, the stored size is the
closed file's size, and a failing write records which boundary failed. Only cells the state
table changes on purpose are updated (P6), and each update is named where it happens.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.publication import PublicationRefused
from decoy_engine.reasons import Operation
from tests.unit.execution.publication._sink_support import (
    POLICIES,
    sink_factory,
    stages,
    table,
    tree,
)

_REFUSED_ERRNOS = (errno.ENOTEMPTY, errno.EEXIST)


@pytest.mark.parametrize("policy", POLICIES)
def test_p1_a_second_sink_with_the_same_result_fails_with_the_raw_errno(
    policy: str, tmp_path: Path
) -> None:
    with sink_factory(policy, tmp_path) as make:
        first = make()
        first.write("customers", table("A"))
        first.commit()
        before = tree(tmp_path / "out")
        second = make()
        second.write("customers", table("A"))
        with pytest.raises(OSError) as info:
            second.commit()
        second.abort()
    assert info.value.errno in _REFUSED_ERRNOS
    assert tree(tmp_path / "out") == before
    assert stages(tmp_path) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_p2_a_different_result_fails_the_same_way_and_the_first_unit_survives(
    policy: str, tmp_path: Path
) -> None:
    with sink_factory(policy, tmp_path) as make:
        first = make()
        first.write("customers", table("A"))
        first.commit()
        before = tree(tmp_path / "out")
        second = make()
        second.write("customers", table("B"))
        with pytest.raises(OSError) as info:
            second.commit()
        second.abort()
    assert info.value.errno in _REFUSED_ERRNOS
    assert tree(tmp_path / "out") == before
    assert pq.read_table(tmp_path / "out" / "customers.parquet").equals(table("A"))


@pytest.mark.parametrize("policy", POLICIES)
def test_p3_p5_a_table_sink_write_and_commit_make_no_fsync(
    policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = os.fsync

    def spy(fd: int) -> None:
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    probe = os.open(tmp_path, os.O_RDONLY)
    try:
        os.fsync(probe)
    finally:
        os.close(probe)
    assert len(calls) == 1  # non-vacuity: the spy sees an fsync made through `os`
    calls.clear()
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.write("customers", table("A"))
        sink.commit()
    assert calls == []
    assert (tmp_path / "out" / "customers.parquet").is_file()


@pytest.mark.parametrize("policy", POLICIES)
def test_p4_an_empty_commit_leaves_an_empty_directory_a_later_sink_replaces(
    policy: str, tmp_path: Path
) -> None:
    with sink_factory(policy, tmp_path) as make:
        make().commit()
        assert sorted(os.listdir(tmp_path / "out")) == []
        later = make()
        later.write("customers", table("B"))
        later.commit()
    assert sorted(os.listdir(tmp_path / "out")) == ["customers.parquet"]
    assert pq.read_table(tmp_path / "out" / "customers.parquet").equals(table("B"))


@pytest.mark.parametrize("policy", POLICIES)
def test_p6_after_a_commit_the_sink_is_sealed(policy: str, tmp_path: Path) -> None:
    """Updated on purpose by the state table (plan 5.6): the pinned behaviour was a second
    `commit()` returning silently, a `write` accepted into a new stage and its commit failing
    `OSError` 39. Now a sealed sink returns its result and refuses the write."""
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.write("customers", table("A"))
        sink.commit()
        before = tree(tmp_path / "out")
        assert sink.commit() is None
        with pytest.raises(PublicationRefused) as info:
            sink.write("orders", table("B"))
        assert info.value.code == "publication_attempt_sealed"
        assert stages(tmp_path) == []
        sink.abort()
    assert tree(tmp_path / "out") == before
    assert stages(tmp_path) == []


# -- the write half ------------------------------------------------------------------------


@pytest.mark.parametrize("policy", POLICIES)
def test_a_table_written_twice_replaces_its_staged_file(policy: str, tmp_path: Path) -> None:
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.write("customers", table("A"))
        sink.write_batches("customers", iter(table("B").to_batches()), schema=table("B").schema)
        sink.commit()
    assert sorted(os.listdir(tmp_path / "out")) == ["customers.parquet"]
    assert pq.read_table(tmp_path / "out" / "customers.parquet").equals(table("B"))


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("name", ["../x", "a/b", "..", "a/../b"])
def test_a_table_name_must_be_one_component(policy: str, name: str, tmp_path: Path) -> None:
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        with pytest.raises(ValueError, match="single path component"):
            sink.write(name, table("A"))
        with pytest.raises(ValueError, match="single path component"):
            sink.write_batches(name, iter(()), schema=table("A").schema)
        with pytest.raises(ValueError, match="single path component"):
            sink.committed_table_path(name)
        sink.abort()
    assert stages(tmp_path) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_stored_bytes_are_the_closed_files_size_for_both_write_paths(
    policy: str, tmp_path: Path
) -> None:
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.write("a", table("A"))
        sink.write_batches("b", iter(table("B").to_batches()), schema=table("B").schema)
        sink.commit()
    stored = sink.publication_evidence.stored_bytes
    assert stored == {
        "a": (tmp_path / "out" / "a.parquet").stat().st_size,
        "b": (tmp_path / "out" / "b.parquet").stat().st_size,
    }
    path = sink.committed_table_path("a")
    assert (path.parent.name, path.name) == ("out", "a.parquet")


@pytest.mark.parametrize("policy", POLICIES)
def test_a_failing_stream_records_the_write_boundary_and_a_failing_close_the_close(
    policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> Iterator[pa.RecordBatch]:
        yield from table("A").to_batches()
        raise RuntimeError("stream failed")

    with sink_factory(policy, tmp_path) as make:
        sink = make()
        with pytest.raises(RuntimeError, match="stream failed"):
            sink.write_batches("t", boom(), schema=table("A").schema)
        assert sink.publication_evidence.last_operation is Operation.WRITE
        assert "t" not in sink.publication_evidence.stored_bytes
        sink.abort()

        from decoy_engine.fileaccess.adapters import arrow as arrow_adapter

        real = arrow_adapter.parquet_writer

        def failing_close(*args: Any, **kwargs: Any) -> Any:
            writer = real(*args, **kwargs)

            class _Closing:
                def write_batch(self, batch: pa.RecordBatch) -> None:
                    writer.write_batch(batch)

                def close(self) -> None:
                    writer.close()
                    raise OSError(errno.ENOSPC, "no space")

            return _Closing()

        monkeypatch.setattr(arrow_adapter, "parquet_writer", failing_close)
        other = make()
        with pytest.raises(OSError):
            other.write_batches("t", iter(table("A").to_batches()), schema=table("A").schema)
        assert other.publication_evidence.last_operation is Operation.CLOSE
        other.abort()
    assert stages(tmp_path) == []
