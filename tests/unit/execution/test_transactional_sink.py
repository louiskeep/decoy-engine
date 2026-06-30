"""Transactional sink protocol + ParquetTransactionalSink tests (TDD).

These tests were written before the implementation and driven to green. They
cover the two required properties: commit byte-parity (a successful run
produces Parquet files identical to run()) and abort atomicity (a failed run
leaves nothing in the target directory).

Source: execution/_transactional_sink.py + docs/relationships-memory-scaling.md
section 6.1 (Option 2 production note).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter
from decoy_engine.execution._transactional_sink import (
    ParquetTransactionalSink,
    TransactionalSink,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.perf_fixtures.fk_relational import build_fk_relational


def _loader(sources: dict[str, pa.Table]):
    def load(table: str) -> pa.Table:
        return sources[table]

    return load


def _full_frame(adapter, plan, sources, graph, ns, registry):
    return adapter.run(
        plan, sources, registry=registry, relationship_graph=graph, namespace_registry=ns
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_parquet_sink_satisfies_protocol(tmp_path: Path) -> None:
    """ParquetTransactionalSink must satisfy the TransactionalSink protocol so
    callers can type-check against the Protocol rather than the concrete class."""
    assert isinstance(ParquetTransactionalSink(tmp_path / "out"), TransactionalSink)


# ---------------------------------------------------------------------------
# Commit: byte-parity with run()
# ---------------------------------------------------------------------------


def test_file_sink_commit_byte_parity(tmp_path: Path) -> None:
    """On success, ParquetTransactionalSink publishes Parquet files whose
    content is byte-identical (schema + data) to the in-memory run() output."""
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=500, width=2, orphan_frac=0.0)
    graph = fx.graph(OrphanPolicy.PRESERVE)
    target = tmp_path / "out"

    sink = ParquetTransactionalSink(target)
    res = adapter.run_sequential(
        fx.plan,
        _loader(fx.sources),
        registry=fx.registry,
        relationship_graph=graph,
        namespace_registry=fx.namespace_registry,
        sink=sink,
    )

    # run_sequential with a TransactionalSink should not accumulate outputs.
    assert res.outputs == {}

    # Target must exist and contain one file per source table.
    assert target.exists(), "target directory not created on commit"
    published = {p.stem: pq.read_table(p) for p in sorted(target.glob("*.parquet"))}
    assert set(published) == set(fx.sources), "wrong set of tables published"

    full = _full_frame(adapter, fx.plan, fx.sources, graph, fx.namespace_registry, fx.registry)
    for table in full.outputs:
        assert published[table].equals(full.outputs[table], check_metadata=False), (
            f"{table}: published Parquet differs from run() output"
        )


# ---------------------------------------------------------------------------
# Abort: atomicity -- nothing partial reaches the target
# ---------------------------------------------------------------------------


def test_file_sink_abort_atomicity(tmp_path: Path) -> None:
    """On abort (orphan FAIL on the child table), ParquetTransactionalSink must
    publish nothing to the target directory and clean up its staging directory.
    The parent table is emitted to staging before the child fails; abort must
    roll that back so no partial output is visible."""
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=500, width=2, orphan_frac=0.05)
    graph = fx.graph(OrphanPolicy.FAIL)
    target = tmp_path / "out"

    sink = ParquetTransactionalSink(target)
    with pytest.raises(ExecutionError) as exc:
        adapter.run_sequential(
            fx.plan,
            _loader(fx.sources),
            registry=fx.registry,
            relationship_graph=graph,
            namespace_registry=fx.namespace_registry,
            sink=sink,
        )
    assert exc.value.code == "orphan_fk_violation"

    # Nothing must be in the target directory.
    target_files = list(target.glob("*.parquet")) if target.exists() else []
    assert target_files == [], f"partial output in target after abort: {target_files}"

    # The staging directory must be cleaned up (lives in tmp_path as a sibling
    # of target because target.parent == tmp_path).
    staging_dirs = list(tmp_path.glob("_decoy_stage_*"))
    assert staging_dirs == [], f"staging dir not cleaned up after abort: {staging_dirs}"


# ---------------------------------------------------------------------------
# Abort when nothing was written (clean-job edge case)
# ---------------------------------------------------------------------------


def test_file_sink_abort_with_no_writes_is_safe(tmp_path: Path) -> None:
    """abort() before any write() (e.g. the loader itself fails) must not raise
    and must leave no artifacts."""
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    sink.abort()  # must not raise

    assert not target.exists()
    staging_dirs = list(tmp_path.glob("_decoy_stage_*"))
    assert staging_dirs == []


# ---------------------------------------------------------------------------
# Back-compat: existing callable sink still works and remains non-transactional
# ---------------------------------------------------------------------------


def test_callable_sink_back_compat(tmp_path: Path) -> None:
    """A plain Callable sink passed to run_sequential still streams tables out
    exactly once per table (the existing contract) and res.outputs is empty."""
    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=300, width=2, orphan_frac=0.0)
    seen: dict[str, pa.Table] = {}

    def sink(table: str, out: pa.Table) -> None:
        assert table not in seen
        seen[table] = out

    res = adapter.run_sequential(
        fx.plan,
        _loader(fx.sources),
        registry=fx.registry,
        relationship_graph=fx.graph(OrphanPolicy.PRESERVE),
        namespace_registry=fx.namespace_registry,
        sink=sink,
    )
    assert set(seen) == set(fx.sources)
    assert res.outputs == {}
