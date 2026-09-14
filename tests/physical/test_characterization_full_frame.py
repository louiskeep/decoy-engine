"""D3 characterization: `full_frame` driver adapter vs the selected
`ExecutionAdapter` called directly (pandas + polars substrate variants, C3).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.physical.drivers import FullFrameAdapter
from decoy_engine.execution.polars._polars_adapter import PolarsExecutionAdapter
from tests.physical._helpers import build_job, normalize_timing_fields


def _table_specs() -> list[dict[str, object]]:
    return [
        {
            "name": "t",
            "columns": [
                {"name": "id", "strategy": "hash", "namespace": "ns_id"},
                {"name": "note", "strategy": "redact"},
                {"name": "code", "strategy": "truncate", "provider_config": {"length": 3}},
            ],
        }
    ]


def _sources() -> dict[str, pa.Table]:
    return {
        "t": pa.table(
            {
                "id": pa.array(["a1", "a2", None, "a3"], type=pa.string()),
                "note": pa.array(["secret1", "secret2", "secret3", None], type=pa.string()),
                "code": pa.array(["12345", "6789", None, "999"], type=pa.string()),
            }
        )
    }


def _result_fields(result: object) -> tuple[object, ...]:
    return (
        {name: tbl.to_pydict() for name, tbl in result.outputs.items()},  # type: ignore[attr-defined]
        {name: tbl.schema for name, tbl in result.outputs.items()},  # type: ignore[attr-defined]
        result.warnings,  # type: ignore[attr-defined]
        result.row_errors,  # type: ignore[attr-defined]
        normalize_timing_fields(result.quality_metrics),  # type: ignore[attr-defined]
        result.table_kinds,  # type: ignore[attr-defined]
        result.boundary_conversion_ms >= 0,  # type: ignore[attr-defined]
    )


def test_full_frame_pandas_adapter_byte_identical(tmp_path: Path) -> None:
    job = build_job(tmp_path, _sources(), _table_specs())
    pandas_adapter = PandasExecutionAdapter()
    direct = pandas_adapter.run(
        job.plan,
        job.sources,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
    )
    via_adapter = FullFrameAdapter(pandas_adapter).run(
        job.plan,
        job.sources,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
    )

    assert _result_fields(direct) == _result_fields(via_adapter)
    # timing fields present-and-well-formed by structure, not exact wall-clock
    # value (plan D3): every StrategyTimingRecord has a non-negative duration.
    assert all(t.elapsed_ms >= 0 for t in via_adapter.timings)
    assert len(via_adapter.timings) == len(direct.timings)


def test_full_frame_adapter_scope_bookkeeping(tmp_path: Path) -> None:
    job = build_job(tmp_path, _sources(), _table_specs())
    pandas_adapter = PandasExecutionAdapter()
    seam_adapter = FullFrameAdapter(pandas_adapter)
    assert seam_adapter.last_invocation is None
    seam_adapter.run(
        job.plan,
        job.sources,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
    )
    assert seam_adapter.last_invocation is not None
    assert seam_adapter.last_invocation.tables == ("t",)


def test_full_frame_polars_native(tmp_path: Path) -> None:
    """Polars-native execution (C3): a scalar no-FK job with only
    polars-migrated strategies runs `_is_fully_polars_native`-true."""
    job = build_job(tmp_path, _sources(), _table_specs())
    polars_adapter = PolarsExecutionAdapter()
    direct = polars_adapter.run(
        job.plan,
        job.sources,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
    )
    via_adapter = FullFrameAdapter(polars_adapter).run(
        job.plan,
        job.sources,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
    )
    assert _result_fields(direct) == _result_fields(via_adapter)


def test_full_frame_polars_unmigrated_strategy_rejection(tmp_path: Path) -> None:
    """A strategy the polars adapter has not migrated + `fallback_to_pandas`
    not asked for rejects with `polars_substrate_strategy_unmigrated` (C3),
    identically whether called directly or through the adapter."""
    specs = [
        {
            "name": "t",
            "columns": [
                {
                    "name": "amt",
                    "strategy": "top_code",
                    "provider_config": {"cap": 89, "over_label": "90+"},
                }
            ],
        }
    ]
    sources = {"t": pa.table({"amt": pa.array([3, 17, 42], type=pa.int64())})}
    job = build_job(tmp_path, sources, specs)
    polars_adapter = PolarsExecutionAdapter(fallback_to_pandas=False)
    adapter = FullFrameAdapter(polars_adapter)

    with pytest.raises(ExecutionError) as direct_exc:
        polars_adapter.run(
            job.plan,
            job.sources,
            registry=job.registry,
            relationship_graph=job.graph,
            namespace_registry=job.namespace_registry,
        )
    with pytest.raises(ExecutionError) as via_exc:
        adapter.run(
            job.plan,
            job.sources,
            registry=job.registry,
            relationship_graph=job.graph,
            namespace_registry=job.namespace_registry,
        )

    assert direct_exc.value.code == "polars_substrate_strategy_unmigrated"
    assert via_exc.value.code == direct_exc.value.code


def test_full_frame_adapter_sink_is_not_a_parameter(tmp_path: Path) -> None:
    """`ExecutionAdapter.run` never took a sink parameter; the adapter must
    not add one either (a provided sink would be silently dropped, matching
    the design doc's "sink-ignored" surface, section 7) -- proven here by the
    adapter simply having no such keyword to pass."""
    import inspect

    sig = inspect.signature(FullFrameAdapter.run)
    assert "sink" not in sig.parameters
