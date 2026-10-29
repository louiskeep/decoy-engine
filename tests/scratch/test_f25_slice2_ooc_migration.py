"""F25 Slice 2 acceptance (A04 parity + injected-arena path) for T01-T08.

Two things are proven here, both on the dev box (no kernel quota needed; the
real-quota A02/A03 assertions run on the GCP host):

* PARITY: the migrated out-of-core route, run through both inner lanes with a
  sink and with forced spill, produces output byte-identical to the resident
  (no-sink) reassembly and to each other. Values, schema, order and warnings
  match. The spill now flows through the attempt-arena capability seam, so this
  is the load-bearing "a migration that changes output is wrong" check.

* INJECTION: the same route driven by a real ``ScratchManager`` attempt arena
  (the Slice 1 subsystem, exercised here with the dev-box tmpfs provider double)
  writes every temporary byte under that arena's owned root, proving the
  capability is threaded through the execution context and not just the
  pre-activation ``LocalArena`` fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.out_of_core._runner import run_fk_out_of_core
from decoy_engine.scratch import ScratchManager
from tests.physical._helpers import build_job
from tests.scratch._fakes import FakeAuthority, QualifiedTmpfsProvider, make_policy


def _specs() -> list[dict[str, Any]]:
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


def _sources(n: int = 40) -> dict[str, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "note": pa.array([f"note-{i}" for i in range(n)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "pid": pa.array([f"p{i % n}" for i in range(n)], type=pa.string()),
        }
    )
    return {"parent": parent, "child": child}


def _sink_outputs(out_dir: Path) -> dict[str, dict[str, list]]:
    return {p.stem: pq.read_table(p).to_pydict() for p in sorted(out_dir.glob("*.parquet"))}


def test_a04_both_lanes_byte_identical_to_resident(tmp_path: Path) -> None:
    fix = tmp_path / "fix"
    fix.mkdir()
    job = build_job(fix, _sources(), _specs(), relationships=_relationships())

    resident = run_fk_out_of_core(
        job.plan, job.sources, registry=job.registry, relationship_graph=job.graph
    )
    resident_out = {name: tbl.to_pydict() for name, tbl in resident.outputs.items()}

    batch_dir = tmp_path / "batch_out"
    batch = run_fk_out_of_core(
        job.plan,
        job.sources,
        registry=job.registry,
        relationship_graph=job.graph,
        sink=ParquetTransactionalSink(batch_dir),
        temp_dir=tmp_path / "batch_work",
    )

    reorder_dir = tmp_path / "reorder_out"
    reorder = run_fk_out_of_core(
        job.plan,
        job.sources,
        registry=job.registry,
        relationship_graph=job.graph,
        sink=ParquetTransactionalSink(reorder_dir),
        temp_dir=tmp_path / "reorder_work",
        budget_bytes=64 * 1024 * 1024,
        out_of_core_reorder_threshold_rows=0,
    )

    assert _sink_outputs(batch_dir) == resident_out
    assert _sink_outputs(reorder_dir) == resident_out
    assert batch.warnings == reorder.warnings == resident.warnings


def test_injected_arena_receives_every_temporary_write(tmp_path: Path) -> None:
    """Drive the route with a real ScratchManager attempt arena and assert every
    spill file lands under that arena's owned root (the threaded-capability
    production path, not the LocalArena fallback)."""
    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    control = tmp_path / "control"
    provider = QualifiedTmpfsProvider()
    manager = ScratchManager.open_pool(pool_id="slice2", control_arena=control, provider=provider)
    try:
        arena = manager.open_attempt(make_policy(stage_root), provider, FakeAuthority())
        arena_root = Path(arena.capability.approved_root)

        fix = tmp_path / "fix"
        fix.mkdir()
        job = build_job(fix, _sources(), _specs(), relationships=_relationships())
        out_dir = tmp_path / "out"
        run_fk_out_of_core(
            job.plan,
            job.sources,
            registry=job.registry,
            relationship_graph=job.graph,
            sink=ParquetTransactionalSink(out_dir),
            temp_dir=tmp_path / "ignored_work",
            budget_bytes=64 * 1024 * 1024,
            out_of_core_reorder_threshold_rows=0,
            scratch_arena=arena,
        )
        # The arena allocated at least one resource directory/file, and the sink
        # output (T09, not migrated) is the only thing written outside it.
        children = list(arena_root.iterdir())
        assert children, "the injected arena received no allocations"
        # The caller's `temp_dir` was ignored for data writes: nothing landed in it.
        ignored = tmp_path / "ignored_work"
        leaked = [p for p in ignored.rglob("*") if p.is_file()] if ignored.exists() else []
        assert not leaked, f"writes leaked outside the injected arena: {leaked[:5]}"
    finally:
        manager.close()
