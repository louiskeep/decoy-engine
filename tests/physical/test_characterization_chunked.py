"""D3 characterization: the THREE `chunked` driver surfaces vs their
production delegates called directly (plan C1: `run_mask_pipeline_chunked`
and `run_native_or_oracle_chunked` are lazy `Iterator[pa.Table]`;
`_pipeline_route_exec.run_mask_chunked` is the resident aggregator
`run_pipeline` actually calls).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._pipeline_route_exec import run_mask_chunked
from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked
from decoy_engine.execution.physical.drivers import (
    MaskPipelineChunkedAdapter,
    NativeOrOracleChunkedAdapter,
    ResidentChunkedAggregatorAdapter,
)
from decoy_engine.providers_v2 import get_default_registry
from tests.physical._helpers import build_job, normalize_timing_fields


def _table_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "t",
            "columns": [
                {"name": "id", "strategy": "hash", "namespace": "ns"},
                {"name": "note", "strategy": "redact"},
                {"name": "code", "strategy": "truncate", "provider_config": {"length": 3}},
            ],
        }
    ]


def _source_table(n: int = 6) -> pa.Table:
    return pa.table(
        {
            "id": pa.array([f"id{i}" for i in range(n)], type=pa.string()),
            "note": pa.array([f"secret{i}" for i in range(n)], type=pa.string()),
            "code": pa.array([f"CODE{i:03d}" for i in range(n)], type=pa.string()),
        }
    )


def _slices(source: pa.Table, chunk_size_rows: int) -> list[pa.Table]:
    return [
        source.slice(start, chunk_size_rows) for start in range(0, source.num_rows, chunk_size_rows)
    ]


def test_mask_pipeline_chunked_adapter_byte_identical(tmp_path: Path) -> None:
    source = _source_table()
    job = build_job(tmp_path, {"t": source}, _table_specs())
    registry = get_default_registry()

    direct_chunks = list(
        run_mask_pipeline_chunked(
            job.config, iter(_slices(source, 2)), table="t", engine_version="v", registry=registry
        )
    )
    via_chunks = list(
        MaskPipelineChunkedAdapter().run(
            job.config, iter(_slices(source, 2)), table="t", engine_version="v", registry=registry
        )
    )

    assert len(direct_chunks) == len(via_chunks)
    for d, v in zip(direct_chunks, via_chunks, strict=True):
        assert d.to_pydict() == v.to_pydict()
        assert d.schema.equals(v.schema)


def test_mask_pipeline_chunked_adapter_is_lazy(tmp_path: Path) -> None:
    """Partial consumption must not force materialization of every chunk
    (plan D3: iterator eagerness/laziness/partial-consumption). The delegate
    peeks exactly one upstream chunk EAGERLY at call time (its own admission
    validation, per `_chunked.py`'s module docstring); this adapter must
    preserve that exact eagerness boundary rather than reading more, or less,
    upstream than the delegate would on its own."""
    source = _source_table(n=6)  # 3 chunks of 2 rows
    job = build_job(tmp_path, {"t": source}, _table_specs())
    registry = get_default_registry()
    pulled: list[int] = []

    def _tracking_slices() -> Any:
        for i, chunk in enumerate(_slices(source, 2)):
            pulled.append(i)
            yield chunk

    iterator = MaskPipelineChunkedAdapter().run(
        job.config, _tracking_slices(), table="t", engine_version="v", registry=registry
    )
    assert pulled == [0]  # one eager admission-validation pull, not all three
    next(iterator)
    pulled_after_first = list(pulled)
    assert pulled_after_first != [0, 1, 2]  # not fully materialized by one output chunk
    list(iterator)
    assert pulled == [0, 1, 2]  # fully drained only once the caller exhausts it


def test_native_or_oracle_chunked_adapter_byte_identical(tmp_path: Path) -> None:
    source = _source_table()
    job = build_job(tmp_path, {"t": source}, _table_specs())

    direct_evidence: list[Any] = []
    via_evidence: list[Any] = []
    direct_chunks = list(
        run_native_or_oracle_chunked(
            job.config,
            iter(_slices(source, 2)),
            table="t",
            engine_version="v",
            route_evidence_sink=direct_evidence,
        )
    )
    via_chunks = list(
        NativeOrOracleChunkedAdapter().run(
            job.config,
            iter(_slices(source, 2)),
            table="t",
            engine_version="v",
            route_evidence_sink=via_evidence,
        )
    )

    assert len(direct_chunks) == len(via_chunks)
    for d, v in zip(direct_chunks, via_chunks, strict=True):
        assert d.to_pydict() == v.to_pydict()
    assert len(direct_evidence) == len(via_evidence) == 1
    # Route decision (native vs oracle) must agree; kernel-call counters are
    # compared structurally (mutate as the iterator drains, both drained here).
    assert direct_evidence[0].native_admitted == via_evidence[0].native_admitted
    assert direct_evidence[0].reroute_reason == via_evidence[0].reroute_reason
    assert direct_evidence[0].compiled_kernel_executed == via_evidence[0].compiled_kernel_executed


def test_resident_chunked_aggregator_byte_identical(tmp_path: Path) -> None:
    source = _source_table()
    job = build_job(tmp_path, {"t": source}, _table_specs())
    registry = get_default_registry()

    direct = run_mask_chunked(
        job.config,
        source,
        table="t",
        engine_version="v",
        registry=registry,
        adapter=None,
        vault_writer=None,
        chunk_size_rows=2,
    )
    via = ResidentChunkedAggregatorAdapter().run(
        job.config,
        source,
        table="t",
        engine_version="v",
        registry=registry,
        adapter=None,
        vault_writer=None,
        chunk_size_rows=2,
    )

    d_outputs, d_timings, d_conv, d_warnings, d_quality = direct
    v_outputs, v_timings, v_conv, v_warnings, v_quality = via

    assert set(d_outputs) == set(v_outputs) == {"t"}
    assert d_outputs["t"].to_pydict() == v_outputs["t"].to_pydict()
    assert d_warnings == v_warnings
    assert normalize_timing_fields(d_quality) == normalize_timing_fields(v_quality)
    assert len(d_timings) == len(v_timings)
    assert isinstance(d_conv, float) and isinstance(v_conv, float)
    assert d_conv >= 0 and v_conv >= 0
