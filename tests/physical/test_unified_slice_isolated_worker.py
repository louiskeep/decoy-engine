"""Task 4.5 D9: ISOLATED-WORKER.

`_isolated_worker.py`'s spawned child ALWAYS hands `run_pipeline` a
`ParquetTransactionalSink` (`_isolated_worker.py:144-168`), so the unified
slice's own admission predicate (`sink is None` required) declines on that
path by construction -- the same as the legacy full_frame branch takes,
unaffected by `unified_slice_enabled`. This asserts that classification
(`_isolated_worker.py:217-226`'s resident-vs-streamed staging choice) is
byte-for-byte unchanged with the flag on and off, over a REAL spawned
child (not the in-process `isolate=False` fallback, which never sets a
sink at all and so is not the shape this gate is about).
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import IsolatedRunResult, run_pipeline_isolated

ENGINE_VERSION = "unified-slice-isolated-worker-test"


def _config(tmp_path) -> dict:
    raw = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {
            "t": {"type": "file", "format": "csv", "path": str(tmp_path / "t.csv")},
        },
        "tables": [{"name": "t", "columns": [{"name": "c", "strategy": "passthrough"}]}],
        "targets": {
            "t": {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")},
        },
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _sources(tmp_path) -> dict[str, pa.Table]:
    df = pd.DataFrame({"c": [f"v{i}" for i in range(20)]})
    df.to_csv(tmp_path / "t.csv", index=False)
    return {"t": pa.Table.from_pandas(df, preserve_index=False)}


@pytest.mark.parametrize("unified_slice_enabled", [False, True])
def test_isolated_worker_classification_unchanged_by_the_flag(
    tmp_path, unified_slice_enabled: bool
) -> None:
    cfg = _config(tmp_path)
    sources = _sources(tmp_path)

    result = run_pipeline_isolated(
        cfg,
        sources,
        engine_version=ENGINE_VERSION,
        unified_slice_enabled=unified_slice_enabled,
    )

    assert isinstance(result, IsolatedRunResult)
    assert result.outcome == "completed"
    assert result.isolated is True
    execution = result.quality_metrics.get("execution") or {}
    # The sink the spawned child always supplies (`ParquetTransactionalSink`)
    # makes the unified slice's own `sink is None` admission check decline
    # regardless of the flag -- so the resident-vs-streamed classification
    # must read identically to the pre-4.5 baseline in both arms.
    assert execution.get("outputs_streamed") is False
    assert execution.get("loaded_fully_in_memory") is True
    assert "unified_slice_activation" not in result.quality_metrics
    assert result.outputs is not None
    assert result.outputs["t"].column("c").to_pylist() == [f"v{i}" for i in range(20)]
