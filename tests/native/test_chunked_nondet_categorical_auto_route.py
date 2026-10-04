"""C1b-ii auto-router regression: a seeded non-deterministic categorical never crashes a job.

The dennis build-gate found that a seeded non-deterministic categorical over a non-string
source ran full-frame before C1b-ii but hard-errored once `run_pipeline` auto-routed it to
the chunked route. `auto_chunk` is a transparent optimization, so every source dtype must
succeed and equal the full-frame run of the identical config on values, column order, Arrow
field types and route evidence. The dispatcher drops schema-level metadata
(`_pipeline_finalize.py`), so literal IPC/schema-metadata identity is not asserted.
A null-typed FIRST chunk followed by a typed one is the pre-existing
`chunked_leading_null_type` exception that every chunked strategy shares.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_pipeline
from decoy_engine.execution._errors import ExecutionError
from tests.native._b8_support import run_one
from tests.native._chunked_categorical_support import cat_col, make_config, passthrough
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    key_provider,
)

_ROWS = 10
_STRINGS = ["a", "b", "c", "a", "b", "c", "a", "b", "c", "a"]


def _source(kind: str) -> pa.Table:
    column = {
        "string": pa.array(_STRINGS, pa.string()),
        "int64": pa.array(range(_ROWS), pa.int64()),
        "float64": pa.array([float(i) for i in range(_ROWS)], pa.float64()),
        "null": pa.nulls(_ROWS),
    }[kind]
    return pa.table({"c": column, "p": pa.array(range(_ROWS), pa.int64())})


def _config(kind: str, tmp_path: Path, weighted: bool) -> dict[str, Any]:
    cfg = make_config([cat_col(mode=None, weighted=weighted), passthrough("p")])
    path = str(tmp_path / f"{kind}.parquet")
    pq.write_table(_source(kind), path)
    cfg["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    cfg["targets"][TABLE] = {"type": "file", "format": "parquet", "path": path + ".out"}
    return cfg


def _run(cfg: dict[str, Any], kind: str, **kw: Any) -> Any:
    return run_pipeline(
        copy.deepcopy(cfg),
        {TABLE: _source(kind)},
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        **kw,
    )


def _c_evidence(result: Any) -> dict[str, Any]:
    route = result.quality_metrics["chunked_route"]
    return {c["column"]: c for c in route["columns"]}["c"]


@NEEDS_COMPANION
@pytest.mark.parametrize("weighted", [False, True], ids=["uniform", "weighted"])
@pytest.mark.parametrize("kind", ["string", "int64", "float64", "null"])
def test_the_auto_routed_job_succeeds_and_equals_the_full_frame_run(
    kind: str, weighted: bool, tmp_path: Path
) -> None:
    cfg = _config(kind, tmp_path, weighted)
    auto = _run(cfg, kind, auto_chunk_threshold_rows=3, chunk_size_rows=4)
    full = _run(cfg, kind, auto_chunk=False)
    assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert full.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    a, f = auto.outputs[TABLE], full.outputs[TABLE]
    assert a.schema.names == f.schema.names
    assert [fl.type for fl in a.schema] == [fl.type for fl in f.schema]
    assert a.column("c").to_pylist() == f.column("c").to_pylist()
    assert a.column("p").to_pylist() == f.column("p").to_pylist()
    if kind != "null":  # an all-null source keeps its null type on both routes
        assert a.schema.field("c").type == pa.string()


@NEEDS_COMPANION
@pytest.mark.parametrize("kind", ["string", "int64", "float64", "null"])
def test_string_runs_the_native_leg_and_every_non_string_runs_the_oracle_leg(
    kind: str, tmp_path: Path
) -> None:
    auto = _run(
        _config(kind, tmp_path, False), kind, auto_chunk_threshold_rows=3, chunk_size_rows=4
    )
    route = auto.quality_metrics["chunked_route"]
    evidence = _c_evidence(auto)
    if kind == "string":
        assert route["native_admitted"] is True
        assert evidence["executed_backend"] == "rust_companion"
    else:
        assert route["native_admitted"] is False
        assert "categorical_source_type_not_string:c:" in route["reroute_reason"]
        assert evidence["executed_backend"] == "pandas_oracle"


@NEEDS_COMPANION
@pytest.mark.parametrize("kind", ["int64", "float64", "null"])
def test_the_route_evidence_matches_the_direct_chunked_entry(kind: str, tmp_path: Path) -> None:
    auto = _run(
        _config(kind, tmp_path, False), kind, auto_chunk_threshold_rows=3, chunk_size_rows=4
    )
    ev: list[Any] = []
    run_one(
        make_config([cat_col(mode=None), passthrough("p")]),
        [_source(kind).slice(0, 4), _source(kind).slice(4, 4), _source(kind).slice(8)],
        route_evidence_sink=ev,
    )
    assert auto.quality_metrics["chunked_route"]["reroute_reason"] == ev[0].reroute_reason


def test_a_null_typed_first_chunk_then_typed_keeps_the_pre_existing_exception() -> None:
    chunks = [
        pa.table({"c": pa.nulls(3), "p": pa.array([0, 1, 2], pa.int64())}),
        pa.table({"c": pa.array(["a", "b", "c"]), "p": pa.array([3, 4, 5], pa.int64())}),
    ]
    with pytest.raises(ExecutionError) as info:
        run_one(make_config([cat_col(mode=None), passthrough("p")]), chunks)
    assert info.value.code == "chunked_leading_null_type"
    # The deterministic variant raises the identical error: not a categorical regression.
    with pytest.raises(ExecutionError) as det:
        run_one(make_config([cat_col(), passthrough("p")]), chunks)
    assert det.value.code == "chunked_leading_null_type"
