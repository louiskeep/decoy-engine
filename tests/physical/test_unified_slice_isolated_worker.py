"""Task 4.5 D9 + route activation (2026-09-20): ISOLATED-WORKER.

`_isolated_worker.py`'s spawned child ALWAYS hands `run_pipeline` a
`ParquetTransactionalSink` (`_isolated_worker.py:225`). Before activation the
unified slice declined on `sink is not None`; after activation the sink is no
longer a decline (it is inert on the full-frame route), so admission is decided
by the rest of the predicate.

Two shapes are exercised over a REAL spawned child (not the in-process
`isolate=False` fallback, which never sets a sink):

- A CSV source stays NON-admissible (admission requires a Parquet file source),
  so its resident-vs-streamed staging classification is byte-for-byte unchanged
  with the flag on and off, and no activation leaf appears.
- A Parquet source in the admitted domain now ACTIVATES when the flag is omitted
  (the new default) and matches the explicit-`False` legacy run exactly, staged
  through `_finalize_outputs` (never the sink). This is the sink-present
  certification extension: the D9 parity arms carry no sink, so this is the first
  proof the sink-bearing worker path is output-equivalent.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
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
    # A CSV source is non-admissible (admission requires a Parquet file source),
    # so the unified slice declines regardless of the flag -- the resident-vs-
    # streamed classification must read identically in both arms, and no
    # activation leaf appears.
    assert execution.get("outputs_streamed") is False
    assert execution.get("loaded_fully_in_memory") is True
    assert "unified_slice_activation" not in result.quality_metrics
    assert result.outputs is not None
    assert result.outputs["t"].column("c").to_pylist() == [f"v{i}" for i in range(20)]


def _parquet_config(tmp_path) -> dict:
    raw = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {
            "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.parquet")},
        },
        "tables": [{"name": "t", "columns": [{"name": "c", "strategy": "passthrough"}]}],
        "targets": {
            "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "out.parquet")},
        },
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _parquet_sources(tmp_path) -> dict[str, pa.Table]:
    table = pa.table({"c": pa.array([f"v{i}" for i in range(20)], type=pa.string())})
    pq.write_table(table, tmp_path / "t.parquet")
    return {"t": table}


def test_isolated_worker_parquet_activates_and_matches_legacy(tmp_path) -> None:
    # The sink-present certification extension. A Parquet source in the admitted
    # domain, run through a REAL spawned worker (which always attaches a
    # ParquetTransactionalSink): the omitted-flag arm takes the new default (the
    # unified lane activates), the explicit-False arm takes the legacy route, and
    # the two are output-equivalent. Staging goes through `_finalize_outputs` in
    # both (outputs resident, not streamed through the sink).
    cfg = _parquet_config(tmp_path)
    sources = _parquet_sources(tmp_path)

    on = run_pipeline_isolated(cfg, sources, engine_version=ENGINE_VERSION)
    off = run_pipeline_isolated(
        cfg, sources, engine_version=ENGINE_VERSION, unified_slice_enabled=False
    )

    for result in (on, off):
        assert isinstance(result, IsolatedRunResult)
        assert result.outcome == "completed"
        assert result.isolated is True
        execution = result.quality_metrics.get("execution") or {}
        # Both stage the resident outputs via `_finalize_outputs`, not the sink.
        assert execution.get("outputs_streamed") is False
        assert execution.get("loaded_fully_in_memory") is True

    expected = [f"v{i}" for i in range(20)]
    assert on.outputs["t"].column("c").to_pylist() == expected
    assert off.outputs["t"].column("c").to_pylist() == expected
    # Staged-output schema + values are identical across the two routes.
    assert on.outputs["t"].schema.equals(off.outputs["t"].schema, check_metadata=True)
    assert on.outputs["t"].equals(off.outputs["t"])
    assert on.table_kinds == off.table_kinds

    # The omitted-flag (default-on) arm carries the activation leaf; the explicit-
    # False arm does not. Every other quality_metrics key matches.
    assert "unified_slice_activation" in on.quality_metrics
    assert "unified_slice_activation" not in off.quality_metrics
    leaf = on.quality_metrics["unified_slice_activation"]
    assert leaf["activated"] is True
    # The activation leaf covers the admitted node(s), each executed.
    assert leaf["nodes"], "activation evidence must cover at least one node"
    assert all(evidence["executed"] is True for evidence in leaf["nodes"].values())
    # Every other quality_metrics entry is value-identical to the legacy route:
    # the D9 parity contract (`_assert_quality_metrics_parity`) drops ONLY the
    # activation leaf and then asserts full equality, so the same holds here.
    on_without_leaf = {
        k: v for k, v in on.quality_metrics.items() if k != "unified_slice_activation"
    }
    assert on_without_leaf == off.quality_metrics
