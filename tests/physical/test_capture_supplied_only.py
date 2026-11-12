"""Capture builds its declared sources through the call the live route makes, so a table the
caller supplies with no descriptor is profiled, seeded and planned the same way."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution.physical import capture_physical_plan_inputs, compile_physical_plan
from tests._declared_schema import declare_rest

SRC = pa.table({"note": ["a", "b", "c"], "g": ["x", "x", "y"]})


def _config(tmp_path: Path) -> dict[str, Any]:
    pq.write_table(SRC, tmp_path / "t.parquet")
    tables = [
        {
            "name": n,
            "columns": [
                {"name": "note", "strategy": "redact"},
                {"name": "g", "strategy": "passthrough"},
            ],
        }
        for n in ("t", "s")
    ]
    return PipelineConfig.model_validate(
        declare_rest(
            {
                "version": 2,
                "global_settings": {"seed": 1},
                "sources": {
                    "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.parquet")}
                },
                "targets": {
                    n: {"type": "file", "format": "parquet", "path": str(tmp_path / f"{n}.out")}
                    for n in ("t", "s")
                },
                "tables": tables,
            }
        )
    ).model_dump()


def test_supplied_only_table_is_profiled_seeded_and_planned_like_the_live_route(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    sources = {"t": SRC, "s": SRC}  # `s` has no descriptor
    live = run_pipeline(config, sources, execution_mode="full_frame", engine_version="cap")
    inputs = capture_physical_plan_inputs(config, sources, engine_version="cap")
    physical = compile_physical_plan(inputs)

    expected = set(live.outputs)
    assert expected == {"t", "s"}
    assert {t.name for t in inputs.profile.tables} == expected
    assert {name for name, _ in inputs.plan.seed_envelope.per_table} == expected
    assert {t.table for t in physical.tables} == expected
