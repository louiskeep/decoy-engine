"""D4 disconnection proof, part 2/2: "bomb" adapters.

`tests/sentry/test_physical_seam_disconnection.py` proves STATICALLY that no
production module's source imports `execution.physical`. This file proves it
DYNAMICALLY: every driver adapter's `run` is patched to raise if ever called,
then a representative `run_pipeline` invocation is forced down each of the
six drivers (full_frame, sequential, chunked, native_stream, out_of_core,
synthesis+mask). None of the patched methods fire -- if the production seam
were ever wired in (Task 4.5+ done by accident today), one of these would
explode instead of silently passing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution.physical.drivers import (
    FullFrameAdapter,
    MaskPipelineChunkedAdapter,
    NativeOrOracleChunkedAdapter,
    NativeStreamAdapter,
    OutOfCoreAdapter,
    ResidentChunkedAggregatorAdapter,
    SequentialAdapter,
    SynthesisStageAdapter,
)
from decoy_engine.profile._readers import LazySource


class _SeamInvokedInProductionError(AssertionError):
    """Raised by the bomb patch; a distinct type so a test failure is
    unambiguous about which invariant broke."""


@pytest.fixture(autouse=True)
def _arm_bomb_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bomb(self: object, *args: Any, **kwargs: Any) -> Any:
        raise _SeamInvokedInProductionError(
            f"{type(self).__name__}.run was invoked while exercising a production route"
        )

    for cls in (
        FullFrameAdapter,
        SequentialAdapter,
        MaskPipelineChunkedAdapter,
        NativeOrOracleChunkedAdapter,
        ResidentChunkedAggregatorAdapter,
        NativeStreamAdapter,
        OutOfCoreAdapter,
        SynthesisStageAdapter,
    ):
        monkeypatch.setattr(cls, "run", _bomb)


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def test_full_frame_route_never_invokes_the_seam(tmp_path: Path) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": "redact"}]}],
        }
    ).model_dump()

    result = run_pipeline(
        config, {"t": source}, engine_version="bomb-test", execution_mode="full_frame"
    )
    assert result.outputs["t"].num_rows == 2


def test_sequential_route_never_invokes_the_seam(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    parent_path = _write(tmp_path, parent, "parent")
    child_path = _write(tmp_path, child, "child")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": str(parent_path)},
                "child": {"type": "file", "format": "parquet", "path": str(child_path)},
            },
            "targets": {
                "parent": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "parent.out.parquet"),
                },
                "child": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "child.out.parquet"),
                },
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}],
                },
                {
                    "name": "child",
                    "columns": [{"name": "pid", "strategy": "hash", "namespace": "n"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                }
            ],
        }
    ).model_dump()

    result = run_pipeline(
        config,
        {"parent": parent, "child": child},
        engine_version="bomb-test",
        execution_mode="sequential",
    )
    assert set(result.outputs) == {"parent", "child"}


def test_chunked_route_never_invokes_the_seam(tmp_path: Path) -> None:
    source = pa.table({"note": pa.array([f"s{i}" for i in range(10)], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": "redact"}]}],
        }
    ).model_dump()

    result = run_pipeline(
        config,
        {"t": source},
        engine_version="bomb-test",
        execution_mode="auto",
        auto_chunk=True,
        auto_chunk_threshold_rows=1,
        chunk_size_rows=3,
    )
    assert result.outputs["t"].num_rows == 10


def test_native_stream_route_never_invokes_the_seam(tmp_path: Path) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": "redact"}]}],
        }
    ).model_dump()

    result = run_pipeline(
        config,
        {"t": LazySource(path=path)},
        engine_version="bomb-test",
        native_route_enabled=True,
        execution_mode="auto",
    )
    assert result.native_route is not None and result.native_route.admitted is True


def test_out_of_core_route_never_invokes_the_seam(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    parent_path = _write(tmp_path, parent, "parent")
    child_path = _write(tmp_path, child, "child")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": str(parent_path)},
                "child": {"type": "file", "format": "parquet", "path": str(child_path)},
            },
            "targets": {
                "parent": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "parent.out.parquet"),
                },
                "child": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "child.out.parquet"),
                },
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}],
                },
                {
                    "name": "child",
                    "columns": [{"name": "pid", "strategy": "hash", "namespace": "n"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                }
            ],
        }
    ).model_dump()

    result = run_pipeline(
        config,
        {"parent": parent, "child": child},
        engine_version="bomb-test",
        execution_mode="out_of_core",
    )
    assert set(result.outputs) == {"parent", "child"}


def test_generate_and_mask_route_never_invokes_the_seam(tmp_path: Path) -> None:
    mask_source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    mask_path = _write(tmp_path, mask_source, "masked")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"masked": {"type": "file", "format": "parquet", "path": str(mask_path)}},
            "targets": {
                "masked": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "masked.out.parquet"),
                },
                "people": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "people.out.csv"),
                },
            },
            "tables": [
                {"name": "masked", "columns": [{"name": "note", "strategy": "redact"}]},
                {
                    "name": "people",
                    "row_count": 3,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
                },
            ],
        }
    ).model_dump()

    result = run_pipeline(config, {"masked": mask_source}, engine_version="bomb-test")
    assert set(result.outputs) == {"masked", "people"}
