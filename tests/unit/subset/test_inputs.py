"""The three input kinds read the same key frames, and only the key columns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
import pyarrow as pa
import pytest

from decoy_engine.profile import LazySource
from decoy_engine.subset import SubsetSource
from decoy_engine.subset._inputs import (
    LazyInput,
    ParquetPathInput,
    ResidentInput,
    as_input,
)
from decoy_engine.subset._keys import RI
from tests.unit.subset._fixtures_inputs import PARENT, csv_source, parquet_source


@dataclass(frozen=True)
class _Recording(LazySource):
    calls: list = field(default_factory=list, compare=False)

    def iter_batches(self, batch_rows, *, columns=None):
        self.calls.append(columns)
        yield from super().iter_batches(batch_rows, columns=columns)


def test_resident_key_frame_numbers_rows_from_zero_and_keeps_requested_order() -> None:
    frame = ResidentInput("parent", pa.table(PARENT)).key_frame(["label", "id"])
    assert frame.columns == [RI, "label", "id"]
    assert frame[RI].to_list() == [0, 1, 2, 3, 4]
    assert frame["id"].to_list() == PARENT["id"]


def test_no_columns_gives_an_index_only_frame_for_every_kind(tmp_path: Path) -> None:
    kinds = [
        ResidentInput("parent", pa.table(PARENT)),
        LazyInput("parent", csv_source(tmp_path, "parent", PARENT), 2),
        LazyInput("parent", parquet_source(tmp_path, "parent", PARENT), 2),
    ]
    for kind in kinds:
        frame = kind.key_frame([])
        assert frame.columns == [RI]
        assert frame[RI].to_list() == [0, 1, 2, 3, 4]
        assert kind.num_rows() == 5


def test_lazy_key_frame_reads_only_the_named_columns_across_batches(tmp_path: Path) -> None:
    src = parquet_source(tmp_path, "parent", PARENT)
    rec = _Recording(src.path, src.access)
    frame = LazyInput("parent", rec, 2).key_frame(["id"])
    assert rec.calls == [["id"]]
    assert frame["id"].to_list() == PARENT["id"]
    assert frame[RI].to_list() == [0, 1, 2, 3, 4]


def test_schemas_are_the_real_types(tmp_path: Path) -> None:
    csv = LazyInput("parent", csv_source(tmp_path, "parent", PARENT), 2)
    res = ResidentInput("parent", pa.table({"k": pa.array([1], pa.int64())}))
    assert csv.polars_schema()["id"] == pl.String
    assert res.polars_schema()["k"] == pl.Int64


def test_parquet_path_input_reads_keys_and_filters_rows(tmp_path: Path) -> None:
    path = parquet_source(tmp_path, "parent", PARENT).path
    inp = ParquetPathInput("parent", SubsetSource(path=str(path), format="parquet"))
    assert inp.num_rows() == 5
    assert inp.key_frame(["id"])["id"].to_list() == PARENT["id"]
    assert inp.filtered_frame([1, 3])["id"].to_list() == ["b", "d"]


def test_as_input_dispatches_on_exact_kinds_and_rejects_the_rest(tmp_path: Path) -> None:
    src = parquet_source(tmp_path, "p", PARENT)
    sub = SubsetSource(path=str(src.path), format="parquet")
    assert isinstance(as_input("p", pa.table(PARENT)), ResidentInput)
    assert isinstance(as_input("p", src), LazyInput)
    assert isinstance(as_input("p", sub), ParquetPathInput)
    for bad in (str(src.path), pl.DataFrame(PARENT), {"path": "x"}):
        with pytest.raises(TypeError):
            as_input("p", bad)
