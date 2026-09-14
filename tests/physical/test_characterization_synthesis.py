"""D3 characterization: `synthesis` stage adapter vs `generate_tables` called
directly, plus a proof that a mixed generate+mask job's stitch precedence
through `run_pipeline` is completely unaffected by this seam's mere presence
(the adapter is never on that call path -- see the disconnection suite for
the enforced version of that claim)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution import run_pipeline
from decoy_engine.execution.physical.drivers import SynthesisStageAdapter
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import compile_plan
from decoy_engine.profile import Profile


def _empty_profile() -> Profile:
    from datetime import datetime, timezone

    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="test",
    )


def _generate_config(row_count: int = 24) -> dict[str, Any]:
    return {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {},
        "tables": [
            {
                "name": "people",
                "row_count": row_count,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 1000, "step": 1},
                    {"name": "tier", "type": "categorical", "categories": ["A", "B", "C"]},
                ],
            }
        ],
        "targets": {"people": {"type": "file", "format": "csv", "path": "out.csv"}},
    }


def test_synthesis_stage_adapter_byte_identical() -> None:
    config = _generate_config()
    plan = compile_plan(config, _empty_profile(), decoy_engine_version="test")

    direct = generate_tables(plan)
    via_adapter = SynthesisStageAdapter().run(plan)

    assert set(direct) == set(via_adapter) == {"people"}
    assert direct["people"].to_pydict() == via_adapter["people"].to_pydict()
    assert direct["people"].schema.equals(via_adapter["people"].schema)


def test_synthesis_stage_adapter_scope_bookkeeping() -> None:
    config = _generate_config()
    plan = compile_plan(config, _empty_profile(), decoy_engine_version="test")
    adapter = SynthesisStageAdapter()
    assert adapter.last_invocation is None
    adapter.run(plan)
    assert adapter.last_invocation is not None
    from decoy_engine.execution.physical import DriverId, ExecutionScope

    assert adapter.last_invocation.driver_id == DriverId.SYNTHESIS
    assert adapter.last_invocation.scope == ExecutionScope.SYNTHESIS_STAGE


def test_mixed_generate_and_mask_job_unaffected_by_the_seam(tmp_path: Path) -> None:
    """A generate+mask job's `run_pipeline` merge/stitch precedence
    (`_pipeline.py`) never touches this package; this is a plain sanity
    check that importing the physical seam changes nothing about it."""
    mask_source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    mask_path = tmp_path / "mask_src.parquet"
    pq.write_table(mask_source, mask_path)
    config = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"masked": {"type": "file", "format": "parquet", "path": str(mask_path)}},
        "targets": {
            "masked": {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / "masked.out.parquet"),
            },
            "people": {"type": "file", "format": "csv", "path": str(tmp_path / "people.out.csv")},
        },
        "tables": [
            {"name": "masked", "columns": [{"name": "note", "strategy": "redact"}]},
            {
                "name": "people",
                "row_count": 5,
                "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
            },
        ],
    }
    from decoy_engine.config import PipelineConfig

    validated = PipelineConfig.model_validate(config).model_dump()
    result = run_pipeline(validated, {"masked": mask_source}, engine_version="test")

    assert set(result.outputs) == {"masked", "people"}
    assert result.table_kinds == {"masked": "mask", "people": "generate"}
    assert result.outputs["people"].num_rows == 5
