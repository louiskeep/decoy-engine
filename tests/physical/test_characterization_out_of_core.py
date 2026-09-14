"""D3 characterization: `out_of_core` driver adapter vs `run_fk_out_of_core`
called directly -- resident, sink+`batch_join` (default inner driver), and
sink+`reorder` (forced inner driver, plan D3 "both OOC inner routes").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import _route_policy
from decoy_engine.execution.out_of_core._runner import run_fk_out_of_core
from decoy_engine.execution.physical.drivers import OutOfCoreAdapter
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
    n = 8
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "note": pa.array([f"s{i}" for i in range(n)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "pid": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
        }
    )
    return {"parent": parent, "child": child}


def _snapshot(result: Any) -> Any:
    return (
        {name: tbl.to_pydict() for name, tbl in result.outputs.items()},
        result.warnings,
        result.row_errors,
        normalize_timing_fields(result.quality_metrics),
    )


def test_out_of_core_adapter_resident_byte_identical(tmp_path: Path) -> None:
    job = build_job(tmp_path, _sources(), _table_specs(), relationships=_relationships())

    direct = run_fk_out_of_core(
        job.plan, job.sources, registry=job.registry, relationship_graph=job.graph
    )
    via = OutOfCoreAdapter().run(
        job.plan, job.sources, registry=job.registry, relationship_graph=job.graph
    )

    assert _snapshot(direct) == _snapshot(via)
    assert set(direct.outputs) == {"parent", "child"}


def test_out_of_core_adapter_sink_batch_join_identical(tmp_path: Path) -> None:
    job = build_job(tmp_path, _sources(), _table_specs(), relationships=_relationships())
    direct_dir = tmp_path / "direct_sink"
    via_dir = tmp_path / "via_sink"

    direct = run_fk_out_of_core(
        job.plan,
        job.sources,
        registry=job.registry,
        relationship_graph=job.graph,
        sink=ParquetTransactionalSink(direct_dir),
    )
    via = OutOfCoreAdapter().run(
        job.plan,
        job.sources,
        registry=job.registry,
        relationship_graph=job.graph,
        sink=ParquetTransactionalSink(via_dir),
    )

    assert direct.outputs == {} and via.outputs == {}
    assert _snapshot(direct) == _snapshot(via)
    for table in ("parent", "child"):
        direct_written = pq.read_table(direct_dir / f"{table}.parquet")
        via_written = pq.read_table(via_dir / f"{table}.parquet")
        assert direct_written.to_pydict() == via_written.to_pydict()


def test_out_of_core_adapter_sink_reorder_inner_route_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the `reorder` inner driver (`out_of_core_reorder_threshold_rows=0`
    admits every eligible sink table, per `resolve_reorder_threshold_rows`'s
    documented override semantics) and prove the adapter still forwards
    byte-identical output for that route too."""
    job = build_job(tmp_path, _sources(), _table_specs(), relationships=_relationships())
    direct_dir = tmp_path / "direct_sink_reorder"
    via_dir = tmp_path / "via_sink_reorder"
    kwargs: dict[str, Any] = dict(
        registry=job.registry,
        relationship_graph=job.graph,
        budget_bytes=64 * 1024 * 1024,
        temp_disk_budget_bytes=64 * 1024 * 1024,
        out_of_core_reorder_threshold_rows=0,
    )

    # Spy on the real `decide_route` (not stubbed -- both calls below run the
    # genuine decision) to confirm this fixture actually selects `reorder`,
    # not a silent `batch_join` fallback that would make the test vacuous.
    decisions: list[bool] = []
    real_decide_route = _route_policy.decide_route

    def _spy_decide_route(*args: Any, **spy_kwargs: Any) -> Any:
        decision = real_decide_route(*args, **spy_kwargs)
        decisions.append(decision.use_reorder)
        return decision

    monkeypatch.setattr(_route_policy, "decide_route", _spy_decide_route)
    monkeypatch.setattr(
        "decoy_engine.execution.out_of_core._runner.decide_route", _spy_decide_route
    )

    direct = run_fk_out_of_core(
        job.plan, job.sources, sink=ParquetTransactionalSink(direct_dir), **kwargs
    )
    via = OutOfCoreAdapter().run(
        job.plan, job.sources, sink=ParquetTransactionalSink(via_dir), **kwargs
    )

    # `parent` has no incoming edge (`decide_route` returns `use_reorder=False`
    # trivially for it, per its own "not incoming_edges" short-circuit); only
    # `child`'s decision proves this fixture, so at least one True per run
    # (two runs: direct + via) is the correct bar, not every decision.
    assert decisions.count(True) == 2, (
        f"fixture did not actually select the reorder route for `child`: {decisions}"
    )
    assert _snapshot(direct) == _snapshot(via)
    for table in ("parent", "child"):
        direct_written = pq.read_table(direct_dir / f"{table}.parquet")
        via_written = pq.read_table(via_dir / f"{table}.parquet")
        assert direct_written.to_pydict() == via_written.to_pydict()


def test_out_of_core_adapter_scope_bookkeeping(tmp_path: Path) -> None:
    job = build_job(tmp_path, _sources(), _table_specs(), relationships=_relationships())
    adapter = OutOfCoreAdapter()
    assert adapter.last_invocation is None
    adapter.run(job.plan, job.sources, registry=job.registry, relationship_graph=job.graph)
    assert adapter.last_invocation is not None
    assert set(adapter.last_invocation.tables) == {"parent", "child"}
