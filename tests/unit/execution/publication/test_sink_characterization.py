"""`test_sink_characterization`: the reference sink's publish behaviour, pinned before the sink
is split into a write half (`StagedParquetWriter`) and a state table (plan 5.6, 7.2; class 9).

Probes P1 to P6 (plan 1.3) on both POSIX policies, plus the write half's own contract: a table
written twice replaces its staged file, names are single components, the stored size is the
closed file's size, and a failing write records which boundary failed. Only cells the state
table changes on purpose are updated (P6, c1a), and the cells the commit through
`publish_unit` changes on purpose (P1 to P5, and the unit listing now holding the
`_decoy_publication.json` descriptor, c1b); each update is named where it happens.
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.publication import PublicationRefused
from decoy_engine.reasons import Operation
from tests.unit.execution.publication._sink_support import (
    DESCRIPTOR,
    POLICIES,
    sink_factory,
    stages,
    table,
    tree,
)


@pytest.mark.parametrize("policy", POLICIES)
def test_p1_a_second_sink_with_the_same_result_is_already_committed(
    policy: str, tmp_path: Path
) -> None:
    """Updated on purpose by the commit through `publish_unit` (c1b): the pinned behaviour was
    a raw `OSError` 39 (`ENOTEMPTY`). Now the same measured result is `already_committed`,
    whatever the second sink's `operation_id`, and the unit is untouched."""
    with sink_factory(policy, tmp_path) as make:
        first = make()
        first.write("customers", table("A"))
        bound = first.commit()
        before = tree(tmp_path / "out")
        second = make()
        second.write("customers", table("A"))
        again = second.commit()
        second.abort()
    assert again.outcome == "already_committed"
    assert again.operation_id != bound.operation_id  # non-vacuity: two attempts, one result
    assert again.result_id == bound.result_id
    assert tree(tmp_path / "out") == before
    assert stages(tmp_path) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_p2_a_different_result_is_refused_and_the_first_unit_survives(
    policy: str, tmp_path: Path
) -> None:
    """Updated on purpose (c1b): the pinned behaviour was the same raw `OSError` 39 as P1, so a
    caller could not tell a retry from a collision. Now it is `publication_target_occupied`."""
    with sink_factory(policy, tmp_path) as make:
        first = make()
        first.write("customers", table("A"))
        first.commit()
        before = tree(tmp_path / "out")
        second = make()
        second.write("customers", table("B"))
        with pytest.raises(PublicationRefused) as info:
            second.commit()
        second.abort()
    assert (info.value.code, info.value.receipt.outcome) == (
        "publication_target_occupied",
        "refused",
    )
    assert tree(tmp_path / "out") == before
    assert pq.read_table(tmp_path / "out" / "customers.parquet").equals(table("A"))
    assert stages(tmp_path) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX durable publish")
@pytest.mark.parametrize("policy", POLICIES)
def test_p3_p5_a_sink_commit_fsyncs_the_unit(
    policy: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Updated on purpose (c1b): the pinned behaviour was no fsync at all on either policy.
    Now the commit is the durable `publication` publish: every unit file and the unit
    directory are fsynced before the receipt says `published`."""
    calls: list[tuple[int, int]] = []
    real = os.fsync

    def spy(fd: int) -> None:
        st = os.fstat(fd)
        calls.append((st.st_dev, st.st_ino))
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
        receipt = sink.commit()
    leaf = tmp_path / "out"

    def ident(path: Path) -> tuple[int, int]:
        st = os.lstat(path)
        return (st.st_dev, st.st_ino)

    unit = {ident(leaf / "customers.parquet"), ident(leaf / DESCRIPTOR), ident(leaf)}
    assert unit <= set(calls)
    assert (receipt.outcome, receipt.durable) == ("published", True)


@pytest.mark.parametrize("policy", POLICIES)
def test_p4_an_empty_commit_binds_a_unit_a_later_sink_cannot_replace(
    policy: str, tmp_path: Path
) -> None:
    """Updated on purpose (c1b): the pinned behaviour was an empty directory that a later sink
    with a different result silently replaced. Now an empty commit binds a unit holding only
    its descriptor, and a different result is refused."""
    with sink_factory(policy, tmp_path) as make:
        assert make().commit().outcome == "published"
        assert sorted(os.listdir(tmp_path / "out")) == [DESCRIPTOR]
        before = tree(tmp_path / "out")
        later = make()
        later.write("customers", table("B"))
        with pytest.raises(PublicationRefused) as info:
            later.commit()
    assert info.value.code == "publication_target_occupied"
    assert tree(tmp_path / "out") == before


@pytest.mark.parametrize("policy", POLICIES)
def test_p6_after_a_commit_the_sink_is_sealed(policy: str, tmp_path: Path) -> None:
    """Updated on purpose by the state table (plan 5.6): the pinned behaviour was a second
    `commit()` returning silently, a `write` accepted into a new stage and its commit failing
    `OSError` 39. Now a sealed sink returns its result and refuses the write."""
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        sink.write("customers", table("A"))
        receipt = sink.commit()
        before = tree(tmp_path / "out")
        assert sink.commit() == receipt
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
    assert sorted(os.listdir(tmp_path / "out")) == [DESCRIPTOR, "customers.parquet"]
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
