"""E1-0 characterization: `ParquetTransactionalSink` publish, failure and zero-record semantics,
and the output name sets accepted and rejected today.

The sink's directory publish is one `os.replace` (POSIX `rename(2)`): it publishes into an absent
or empty destination and fails closed on a non-empty one. The policy-issued `OutputTxn` must keep
exactly this behavior, so every case below stays green after the port.

Updated on purpose by F22 c1b, where the commit goes through `publish_unit`: a published unit
also holds `_decoy_publication.json`; a non-empty destination is refused
(`publication_target_occupied`) with the stage removed on that exit; a zero-write commit binds a
unit holding only the descriptor and is refused, not silent, onto a non-empty target.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.publication import DESCRIPTOR_NAME, PublicationRefused

TABLE = pa.table({"a": [1, 2, 3]})


def _stages(parent: Path) -> list[str]:
    return sorted(
        p.name for p in parent.iterdir() if p.name.startswith("._decoy_publication_pending_")
    )


def _snapshot(directory: Path) -> dict[str, tuple[int, int, int, int]]:
    out = {}
    for p in sorted(directory.iterdir()):
        st = p.stat()
        out[p.name] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    return out


def test_publish_into_absent_target(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "out"
    sink = ParquetTransactionalSink(target)
    sink.write("orders", TABLE)
    assert not target.exists()
    assert len(_stages(target.parent)) == 1
    sink.commit()
    assert sorted(p.name for p in target.iterdir()) == [DESCRIPTOR_NAME, "orders.parquet"]
    assert pq.read_table(target / "orders.parquet").equals(TABLE)
    assert _stages(target.parent) == []


def test_publish_into_empty_directory(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()
    sink = ParquetTransactionalSink(target)
    sink.write("orders", TABLE)
    sink.commit()
    assert sorted(p.name for p in target.iterdir()) == [DESCRIPTOR_NAME, "orders.parquet"]


def test_non_empty_destination_is_refused_and_left_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()
    (target / "keep.txt").write_text("occupant")
    before = _snapshot(target)
    sink = ParquetTransactionalSink(target)
    sink.write("orders", TABLE)
    assert len(_stages(tmp_path)) == 1  # non-vacuity: the table was staged
    with pytest.raises(PublicationRefused) as info:
        sink.commit()
    assert info.value.code == "publication_target_occupied"
    assert _snapshot(target) == before
    assert (target / "keep.txt").read_text() == "occupant"
    assert _stages(tmp_path) == []  # a refusal removes the stage on that exit
    sink.abort()
    assert _stages(tmp_path) == []
    assert _snapshot(target) == before


def test_zero_write_commit_binds_a_descriptor_only_unit(tmp_path: Path) -> None:
    target = tmp_path / "x" / "out"
    assert ParquetTransactionalSink(target).commit().outcome == "published"
    assert target.is_dir() and [p.name for p in target.iterdir()] == [DESCRIPTOR_NAME]
    assert _stages(target.parent) == []


def test_zero_write_commit_onto_a_non_empty_target_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir()
    (target / "keep.txt").write_text("occupant")
    before = _snapshot(target)
    with pytest.raises(PublicationRefused) as info:
        ParquetTransactionalSink(target).commit()
    assert info.value.code == "publication_target_occupied"
    assert _snapshot(target) == before


def test_abort_without_writes_is_a_no_op(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    sink.abort()
    sink.abort()
    assert list(tmp_path.iterdir()) == []


def test_abort_discards_the_staged_tables(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    sink.write("a", TABLE)
    sink.write("b", TABLE)
    sink.abort()
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_failing_batch_stream_publishes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    batch = TABLE.to_batches()[0]

    def boom() -> Iterator[pa.RecordBatch]:
        yield batch
        raise RuntimeError("stream failed")

    with pytest.raises(RuntimeError, match="stream failed"):
        sink.write_batches("t", boom(), schema=TABLE.schema)
    assert not target.exists()
    sink.abort()
    assert list(tmp_path.iterdir()) == []


def test_empty_batch_stream_still_writes_a_zero_row_file(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    sink.write_batches("t", iter(()), schema=TABLE.schema)
    sink.commit()
    out = pq.read_table(target / "t.parquet")
    assert out.num_rows == 0 and out.schema.equals(TABLE.schema)


def test_write_batches_matches_write_bytes(tmp_path: Path) -> None:
    a = ParquetTransactionalSink(tmp_path / "a")
    a.write("t", TABLE)
    a.commit()
    b = ParquetTransactionalSink(tmp_path / "b")
    b.write_batches("t", iter(TABLE.to_batches()), schema=TABLE.schema)
    b.commit()
    assert (tmp_path / "a" / "t.parquet").read_bytes() == (
        tmp_path / "b" / "t.parquet"
    ).read_bytes()


def test_writing_the_same_table_twice_replaces_the_staged_file(tmp_path: Path) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    sink.write("t", pa.table({"a": [1]}))
    sink.write("t", TABLE)
    sink.commit()
    assert pq.read_table(target / "t.parquet").equals(TABLE)


def test_committed_table_path_and_spill_parent(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    assert sink.committed_table_path("orders") == tmp_path / "out" / "orders.parquet"
    assert sink.spill_parent == tmp_path


def test_stage_directory_is_a_sibling_with_the_documented_prefix(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "o" / "out")
    sink.write("t", TABLE)
    (stage,) = _stages(tmp_path / "o")
    assert stage.startswith("._decoy_publication_pending_")
    sink.abort()


# Names the sink accepts today: the leaf is `<name>.parquet`, so the empty name is accepted.
ACCEPTED = ["orders", "a b", "x.y", "-lead", "~x", "con", "a\\b", "", "Ünï", "é" * 100, "n" * 247]
# Rejected by the sink's own validator (not a single path component).
REJECTED_VALUE = [".", "..", "a..b", "a/b", "/abs", "a/../b", "../outside", "..."]


@pytest.mark.parametrize("name", ACCEPTED, ids=lambda n: f"ok-{len(n)}-{n[:6]}")
def test_accepted_table_names_publish_under_the_same_leaf(tmp_path: Path, name: str) -> None:
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    sink.write(name, TABLE)
    sink.commit()
    assert sorted(p.name for p in target.iterdir()) == sorted([DESCRIPTOR_NAME, f"{name}.parquet"])


@pytest.mark.parametrize("name", REJECTED_VALUE)
def test_rejected_table_names_raise_value_error_before_any_file(tmp_path: Path, name: str) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    with pytest.raises(ValueError, match="is not a single path component"):
        sink.write(name, TABLE)
    with pytest.raises(ValueError, match="is not a single path component"):
        sink.write_batches(name, iter(()), schema=TABLE.schema)
    assert list(tmp_path.iterdir()) == []


def test_nul_and_overlong_names_fail_at_write_not_at_validation(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "out")
    with pytest.raises(ValueError, match=r"embedded null (byte|character in path)"):
        sink.write("a\0b", TABLE)
    # A failed write ends the attempt (sink state table, F22 plan 5.6), so the second name is
    # tried on a fresh sink; the first is refused with the code the attempt ended with.
    with pytest.raises(PublicationRefused, match="publication_failed"):
        sink.write("n" * 251, TABLE)
    sink.abort()
    other = ParquetTransactionalSink(tmp_path / "out")
    with pytest.raises(OSError) as info:
        other.write("n" * 251, TABLE)
    assert info.value.errno == errno.ENAMETOOLONG
    other.abort()
    assert os.listdir(tmp_path) == []
