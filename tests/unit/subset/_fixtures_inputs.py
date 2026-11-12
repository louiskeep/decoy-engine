"""Builders shared by the input-seam tests: one FK fixture in three physical forms."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pyarrow as pa

from decoy_engine.profile import LazySource
from decoy_engine.profile._lazy_csv import CsvLazySource
from decoy_engine.subset import FanOutPolicy, SeedSpec, SubsetRequest
from tests.unit.subset.conftest import rel

PARENT = {"id": ["a", "b", "c", "d", "e"], "label": ["p1", "p2", "p3", "p4", "p5"]}
CHILD = {"cid": ["1", "2", "3", "4", "5"], "pid": ["a", "a", "b", "c", "d"]}
LONELY = {"x": ["u", "v"]}


def text_tables() -> dict[str, pa.Table]:
    return {n: pa.table(d) for n, d in (("parent", PARENT), ("child", CHILD), ("lonely", LONELY))}


def request(keys: tuple[str, ...] = ("b", "c")) -> SubsetRequest:
    seed = SeedSpec(
        table="parent", mode="keys", key_columns=("id",), keys=tuple((k,) for k in keys)
    )
    return SubsetRequest(
        relationships=(rel("parent", ("id",), "child", ("pid",)),),
        seeds=(seed,),
        policy=FanOutPolicy(),
    )


def csv_source(tmp_path: Path, name: str, data: dict[str, list[str]]) -> CsvLazySource:
    path = tmp_path / f"{name}.csv"
    cols = list(data)
    lines = [",".join(cols)] + [
        ",".join(data[c][i] for c in cols) for i in range(len(data[cols[0]]))
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    desc = {"type": "file", "format": "csv", "path": str(path), "has_header": True}
    return CsvLazySource.from_descriptor(desc, table=name)


def parquet_source(tmp_path: Path, name: str, data: dict) -> LazySource:
    path = tmp_path / f"{name}.parquet"
    pl.DataFrame(data).write_parquet(path)
    return LazySource(path)
