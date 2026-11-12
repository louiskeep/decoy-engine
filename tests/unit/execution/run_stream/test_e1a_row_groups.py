"""AT-E1a, output grouping: `align_row_groups` keeps one row group per chunk; `auto` regroups
across chunks and keeps its row and byte caps (M-E1a6)."""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution import _chunked_output_sink as out


class _Sink:
    def __init__(self) -> None:
        self.groups: list[int] = []
        self.schema: pa.Schema | None = None

    def write_batches(self, table: str, batches: object, *, schema: pa.Schema) -> None:
        self.schema = schema
        self.groups = [b.num_rows for b in batches]  # type: ignore[attr-defined]


def _chunks(sizes: list[int]) -> list[pa.Table]:
    return [pa.table({"a": pa.array([str(i)] * n)}) for i, n in enumerate(sizes)]


def _groups(sizes: list[int], **kwargs: object) -> list[int]:
    sink = _Sink()
    out.stream_table(sink, "t", _chunks(sizes), fixed_columns=frozenset({"a"}), **kwargs)  # type: ignore[arg-type]
    return sink.groups


def test_auto_regroups_across_chunks_at_the_row_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(out, "ROW_GROUP_ROWS", 4)
    assert _groups([3, 3, 3]) == [4, 4, 1]


def test_aligned_keeps_chunk_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(out, "ROW_GROUP_ROWS", 4)
    assert _groups([3, 3, 3], align_row_groups=True) == [3, 3, 3]


def test_aligned_splits_a_chunk_only_at_the_writer_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(out, "ROW_GROUP_ROWS", 4)
    assert _groups([6, 2], align_row_groups=True) == [4, 2, 2]


def test_auto_regroups_short_chunks_and_aligned_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(out, "ROW_GROUP_ROWS", 100)
    assert _groups([2, 2]) == [4]
    assert _groups([2, 2], align_row_groups=True) == [2, 2]


def test_auto_keeps_the_byte_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(out, "ROW_GROUP_ROWS", 100)
    monkeypatch.setattr(out, "ROW_GROUP_BYTES", 1)
    assert _groups([2, 2]) == [2, 2]


def test_a_zero_row_table_writes_an_empty_table_of_its_schema_when_given_one() -> None:
    sink = _Sink()
    schema = pa.schema([pa.field("a", pa.string())])
    out.stream_table(sink, "t", [], fixed_columns=frozenset({"a"}), empty_schema=schema)
    assert sink.groups == [] and sink.schema == schema
    with pytest.raises(Exception, match="no chunks to join"):
        out.stream_table(_Sink(), "t", [], fixed_columns=frozenset({"a"}))
