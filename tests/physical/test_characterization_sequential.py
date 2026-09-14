"""D3 characterization: `sequential` driver adapter vs `run_sequential`
called directly -- resident, legacy-callable-sink, and transactional-sink
publication shapes (design doc section 7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution._sequential import run_sequential
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.physical.drivers import SequentialAdapter
from tests.physical._helpers import build_job, normalize_timing_fields


def _table_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "parent",
            "columns": [
                {"name": "id", "strategy": "hash", "namespace": "pns"},
                {"name": "note", "strategy": "redact"},
            ],
        },
        {
            "name": "child",
            "columns": [
                {"name": "cid", "strategy": "hash", "namespace": "cns"},
                {"name": "pid", "strategy": "hash", "namespace": "pns"},
            ],
        },
    ]


def _relationships() -> list[dict[str, Any]]:
    return [
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["pid"]}],
            "orphan_policy": "preserve",
            "namespace": "pns",
        }
    ]


def _sources() -> dict[str, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array(["p1", "p2", "p3"], type=pa.string()),
            "note": pa.array(["s1", "s2", None], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array(["c1", "c2", "c3", "c4"], type=pa.string()),
            "pid": pa.array(["p1", "p2", "orphan", "p3"], type=pa.string()),
        }
    )
    return {"parent": parent, "child": child}


def _outputs_snapshot(result: Any) -> Any:
    return (
        {name: tbl.to_pydict() for name, tbl in result.outputs.items()},
        result.warnings,
        result.row_errors,
        normalize_timing_fields(result.quality_metrics),
    )


def test_sequential_adapter_resident_byte_identical(tmp_path: Path) -> None:
    job = build_job(tmp_path, _sources(), _table_specs(), relationships=_relationships())

    def loader(table: str) -> pa.Table:
        return job.sources[table]

    direct = run_sequential(
        PandasExecutionAdapter(),
        job.plan,
        loader,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
    )
    via_adapter = SequentialAdapter().run(
        PandasExecutionAdapter(),
        job.plan,
        loader,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
    )

    assert _outputs_snapshot(direct) == _outputs_snapshot(via_adapter)
    assert set(direct.outputs) == {"parent", "child"}


def test_sequential_adapter_legacy_callable_sink_identical(tmp_path: Path) -> None:
    job = build_job(tmp_path, _sources(), _table_specs(), relationships=_relationships())

    def loader(table: str) -> pa.Table:
        return job.sources[table]

    direct_writes: dict[str, pa.Table] = {}
    via_writes: dict[str, pa.Table] = {}

    direct = run_sequential(
        PandasExecutionAdapter(),
        job.plan,
        loader,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
        sink=lambda table, data: direct_writes.__setitem__(table, data),
    )
    via_adapter = SequentialAdapter().run(
        PandasExecutionAdapter(),
        job.plan,
        loader,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
        sink=lambda table, data: via_writes.__setitem__(table, data),
    )

    # A plain-callable sink means `outputs` stays empty (production contract):
    # publication happened through the sink, not the resident dict.
    assert direct.outputs == {} and via_adapter.outputs == {}
    assert set(direct_writes) == set(via_writes) == {"parent", "child"}
    for table in direct_writes:
        assert direct_writes[table].to_pydict() == via_writes[table].to_pydict()
    assert _outputs_snapshot(direct) == _outputs_snapshot(via_adapter)


def test_sequential_adapter_transactional_sink_identical(tmp_path: Path) -> None:
    job = build_job(tmp_path, _sources(), _table_specs(), relationships=_relationships())

    def loader(table: str) -> pa.Table:
        return job.sources[table]

    direct_dir = tmp_path / "direct_sink"
    via_dir = tmp_path / "via_sink"

    direct = run_sequential(
        PandasExecutionAdapter(),
        job.plan,
        loader,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
        sink=ParquetTransactionalSink(direct_dir),
    )
    via_adapter = SequentialAdapter().run(
        PandasExecutionAdapter(),
        job.plan,
        loader,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
        sink=ParquetTransactionalSink(via_dir),
    )

    assert direct.outputs == {} and via_adapter.outputs == {}
    assert _outputs_snapshot(direct) == _outputs_snapshot(via_adapter)
    for table in ("parent", "child"):
        direct_written = pq.read_table(direct_dir / f"{table}.parquet")
        via_written = pq.read_table(via_dir / f"{table}.parquet")
        assert direct_written.to_pydict() == via_written.to_pydict()


def test_sequential_adapter_scope_bookkeeping(tmp_path: Path) -> None:
    job = build_job(tmp_path, _sources(), _table_specs(), relationships=_relationships())

    def loader(table: str) -> pa.Table:
        return job.sources[table]

    adapter = SequentialAdapter()
    assert adapter.last_invocation is None
    adapter.run(
        PandasExecutionAdapter(),
        job.plan,
        loader,
        registry=job.registry,
        relationship_graph=job.graph,
        namespace_registry=job.namespace_registry,
    )
    assert adapter.last_invocation is not None
    from decoy_engine.execution.physical import DriverId, ExecutionScope

    assert adapter.last_invocation.driver_id == DriverId.SEQUENTIAL
    assert adapter.last_invocation.scope == ExecutionScope.RELATIONSHIP_JOB
