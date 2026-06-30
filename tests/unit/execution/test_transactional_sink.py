"""Transactional sink protocol + ParquetTransactionalSink tests (TDD).

These tests were written before the implementation and driven to green. They
cover the required properties: commit atomicity (a successful run publishes all
tables via a single directory rename), abort atomicity (a failed run leaves
nothing in the target directory), path containment (escaped table names are
rejected), and abort error isolation (abort() errors never mask the original
run failure).

Source: execution/_transactional_sink.py + docs/relationships-memory-scaling.md
section 6.1 (Option 2 production note).
"""

from __future__ import annotations

import os
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


def test_file_sink_commit_value_parity(tmp_path: Path) -> None:
    """On success, ParquetTransactionalSink publishes Parquet files whose
    Arrow schema (field names and types) and row values match the in-memory
    run() output."""
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
        assert published[table].schema.equals(full.outputs[table].schema, check_metadata=False), (
            f"{table}: published Parquet schema differs from run() output"
        )
        assert published[table].equals(full.outputs[table], check_metadata=False), (
            f"{table}: published Parquet values differ from run() output"
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


# ---------------------------------------------------------------------------
# B1/M1/M3: commit failure must publish nothing (atomic directory rename)
# ---------------------------------------------------------------------------


def test_commit_failure_publishes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If os.replace raises during commit, nothing is published to the target
    directory and abort() cleans up staging afterward.

    With the old per-file rename loop, a failure on the Nth rename leaves the
    first N-1 files already in the target (partial publish). With a single
    directory rename, either the whole set lands or nothing does.
    """
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    sink.write("table_a", pa.table({"x": [1]}))
    sink.write("table_b", pa.table({"x": [2]}))

    def _fail_replace(src: object, dst: object) -> None:
        raise OSError("simulated commit failure")

    monkeypatch.setattr(os, "replace", _fail_replace)

    with pytest.raises(OSError, match="simulated commit failure"):
        sink.commit()

    # Target must not exist after a failed commit -- not even an empty directory.
    # (The old per-file commit created the target dir before the rename loop,
    # so a failure left an empty stale target dir; the single-rename design
    # never touches the target path until the one successful rename.)
    assert not target.exists(), "target must not exist after a failed commit"

    # Staging directory must still be present so abort() can clean it up.
    staging_dirs = list(tmp_path.glob("_decoy_stage_*"))
    assert staging_dirs, "staging dir must still exist so abort() can clean it"

    sink.abort()
    staging_dirs = list(tmp_path.glob("_decoy_stage_*"))
    assert staging_dirs == [], f"staging not cleaned up after abort: {staging_dirs}"


# ---------------------------------------------------------------------------
# M2: abort() errors must not mask the original run failure
# ---------------------------------------------------------------------------


def test_run_sequential_abort_error_does_not_mask_original(tmp_path: Path) -> None:
    """If abort() raises during cleanup of a run-loop failure, the original
    exception must propagate, not the abort error."""

    class _RaisyAbortSink:
        """Sink that fails on write and also fails on abort."""

        def write(self, table: str, data: pa.Table) -> None:
            raise RuntimeError("original error")

        def commit(self) -> None:
            pass

        def abort(self) -> None:
            raise RuntimeError("abort error -- must be swallowed")

    adapter = PandasExecutionAdapter()
    fx = build_fk_relational(rows=100, width=2, orphan_frac=0.0)

    with pytest.raises(RuntimeError, match="original error"):
        adapter.run_sequential(
            fx.plan,
            _loader(fx.sources),
            registry=fx.registry,
            relationship_graph=fx.graph(OrphanPolicy.PRESERVE),
            namespace_registry=fx.namespace_registry,
            sink=_RaisyAbortSink(),
        )


# ---------------------------------------------------------------------------
# m2: table-name path containment
# ---------------------------------------------------------------------------


def test_write_rejects_path_traversal_table_name(tmp_path: Path) -> None:
    """write() must reject table names containing path separators or '..' and
    must not create any staging artifacts on rejection."""
    target = tmp_path / "out"
    sink = ParquetTransactionalSink(target)
    data = pa.table({"x": [1, 2, 3]})

    with pytest.raises(ValueError, match="not a single path component"):
        sink.write("../escape", data)

    # No staging directories must have been created on a rejected write.
    staging_dirs = list(tmp_path.glob("_decoy_stage_*"))
    assert staging_dirs == [], f"staging created on rejected write: {staging_dirs}"
