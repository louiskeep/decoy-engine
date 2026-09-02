"""Lifecycle + single-read + FAIL-precount tests for the reorder driver
(P4-A Task 6 acceptance tests #5, #6, #7b of
`docs/plans/2026-09-02-p4-task6-reorder-driver.md` section 4).

Calls `_stream_driver.stream_table` directly (not through the
`run_stream_driver` harness) where a test needs to spy on a SPECIFIC table's
call -- the ExitStack lifecycle, the FAIL-precount connection count, and the
single-read guard all hinge on exactly what happens inside one `stream_table`
call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_driver import stream_table
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge
from tests.parity.test_out_of_core_fk_parity import _assert_value_equal, _build_chain, _run_oracle
from tests.unit.execution._stream_driver_harness import run_stream_driver

_SEED = b"\x66" * 8
_RUN_BYTES_CAP = 8 * 1024 * 1024


def _col(strategy: str, *, namespace: str | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=(),
        coherent_with=(),
    )


# ---------------------------------------------------------------------------
# Shared two-edge (parent_a, parent_b -> child) fixture for #5 / #6.
# ---------------------------------------------------------------------------


def _two_edge_setup(
    policy_a: OrphanPolicy, policy_b: OrphanPolicy, *, n: int = 8, orphan_in_edge2: bool = False
) -> tuple[Any, dict[str, pa.Table], RelationshipEdge, RelationshipEdge]:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("parent_a", TableSeed(per_column=(("id", _col("passthrough")),), per_group=())),
                ("parent_b", TableSeed(per_column=(("id", _col("passthrough")),), per_group=())),
                ("child", TableSeed(per_column=(), per_group=())),
            ),
        )
    )
    edge_a = RelationshipEdge(
        parent_table="parent_a",
        parent_columns=("id",),
        child_table="child",
        child_columns=("a_id",),
        namespace="ns_a",
        orphan_policy=policy_a,
    )
    edge_b = RelationshipEdge(
        parent_table="parent_b",
        parent_columns=("id",),
        child_table="child",
        child_columns=("b_id",),
        namespace="ns_b",
        orphan_policy=policy_b,
    )
    parent_a = pa.table({"id": [f"a{i}" for i in range(n)]})
    parent_b = pa.table({"id": [f"b{i}" for i in range(n)]})
    b_ids = [f"b{i}" for i in range(n)]
    if orphan_in_edge2:
        b_ids[0] = "orphanB"
    child = pa.table({"a_id": [f"a{i}" for i in range(n)], "b_id": b_ids})
    sources = {"parent_a": parent_a, "parent_b": parent_b, "child": child}
    return plan, sources, edge_a, edge_b


def _build_parent_relations(
    plan: Any,
    sources: dict[str, pa.Table],
    edge_a: RelationshipEdge,
    edge_b: RelationshipEdge,
    root: Path,
) -> dict[RelationshipEdge, Any]:
    """Run `parent_a` / `parent_b` (no incoming edges of their own) through
    `stream_table` so `parent_relations` is populated before the test drives
    `child` directly."""
    parent_relations: dict[RelationshipEdge, Any] = {}
    outputs: dict[str, pa.Table] = {}
    warnings: list[Any] = []
    for ptable, pedge in (("parent_a", edge_a), ("parent_b", edge_b)):
        stream_table(
            plan,
            ptable,
            sources[ptable],
            incoming_edges=(),
            outgoing_edges=(pedge,),
            parent_relations=parent_relations,
            temp_dir=root / "joins" / ptable,
            relation_dir=root / "relations" / ptable,
            staging_path=root / "staged" / ptable / "masked_keys.parquet",
            memory_limit=None,
            batch_rows=None,
            run_bytes_cap=_RUN_BYTES_CAP,
            sink=None,
            outputs=outputs,
            warnings=warnings,
        )
    return parent_relations


def _spy_ordered_join(monkeypatch: Any) -> list[Any]:
    """Record every `_OrderedJoinRows` a `StreamFkJoiner.run_ordered_join`
    call successfully returns, across every joiner instance."""
    opened: list[Any] = []
    orig = StreamFkJoiner.run_ordered_join

    def spy(self: StreamFkJoiner, *args: Any, **kwargs: Any) -> Any:
        rows = orig(self, *args, **kwargs)
        opened.append(rows)
        return rows

    monkeypatch.setattr(StreamFkJoiner, "run_ordered_join", spy)
    return opened


class _RecordingSink:
    """A sink that counts `write_batches` calls without doing anything else."""

    def __init__(self) -> None:
        self.calls = 0

    def write(self, table: str, data: pa.Table) -> None:
        raise AssertionError("write() not expected on the streaming path")

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        self.calls += 1
        for _ in batches:
            pass

    def commit(self) -> None:
        pass

    def abort(self) -> None:
        pass


class _OneBatchSink:
    """Consumes exactly one batch, then returns normally without exhausting
    the rest -- the abandonment case where the SINK, not stream_table, drops
    the iterator early."""

    def __init__(self) -> None:
        self.calls = 0

    def write(self, table: str, data: pa.Table) -> None:
        raise AssertionError("write() not expected on the streaming path")

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        it = iter(batches)
        next(it, None)
        self.calls += 1

    def commit(self) -> None:
        pass

    def abort(self) -> None:
        pass


class _RaiseAfterOneBatchSink:
    """Consumes one batch, then raises -- the sink-error abandonment case."""

    def __init__(self) -> None:
        self.calls = 0

    def write(self, table: str, data: pa.Table) -> None:
        raise AssertionError("write() not expected on the streaming path")

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        it = iter(batches)
        next(it, None)
        self.calls += 1
        raise RuntimeError("boom-after-one-batch")

    def commit(self) -> None:
        pass

    def abort(self) -> None:
        pass


# ---------------------------------------------------------------------------
# #5: lifecycle, three distinct abandonment paths.
# ---------------------------------------------------------------------------


def test_lifecycle_sink_consumes_one_batch_then_returns(tmp_path: Path, monkeypatch: Any) -> None:
    plan, sources, edge_a, edge_b = _two_edge_setup(
        OrphanPolicy.PRESERVE, OrphanPolicy.PRESERVE, n=12
    )
    parent_relations = _build_parent_relations(plan, sources, edge_a, edge_b, tmp_path / "parents")
    opened = _spy_ordered_join(monkeypatch)
    sink = _OneBatchSink()
    child_temp_dir = tmp_path / "joins" / "child"

    stream_table(
        plan,
        "child",
        sources["child"],
        incoming_edges=(edge_a, edge_b),
        outgoing_edges=(),
        parent_relations=parent_relations,
        temp_dir=child_temp_dir,
        relation_dir=tmp_path / "relations" / "child",
        staging_path=tmp_path / "staged" / "child" / "masked_keys.parquet",
        memory_limit=None,
        batch_rows=3,
        run_bytes_cap=_RUN_BYTES_CAP,
        sink=sink,
        outputs={},
        warnings=[],
    )

    assert sink.calls == 1
    assert len(opened) == 2  # one _OrderedJoinRows per incoming edge
    assert all(rows._closed for rows in opened)
    assert not child_temp_dir.exists()


def test_lifecycle_sink_raises_after_one_batch(tmp_path: Path, monkeypatch: Any) -> None:
    plan, sources, edge_a, edge_b = _two_edge_setup(
        OrphanPolicy.PRESERVE, OrphanPolicy.PRESERVE, n=12
    )
    parent_relations = _build_parent_relations(plan, sources, edge_a, edge_b, tmp_path / "parents")
    opened = _spy_ordered_join(monkeypatch)
    sink = _RaiseAfterOneBatchSink()
    child_temp_dir = tmp_path / "joins" / "child"

    with pytest.raises(RuntimeError, match="boom-after-one-batch"):
        stream_table(
            plan,
            "child",
            sources["child"],
            incoming_edges=(edge_a, edge_b),
            outgoing_edges=(),
            parent_relations=parent_relations,
            temp_dir=child_temp_dir,
            relation_dir=tmp_path / "relations" / "child",
            staging_path=tmp_path / "staged" / "child" / "masked_keys.parquet",
            memory_limit=None,
            batch_rows=3,
            run_bytes_cap=_RUN_BYTES_CAP,
            sink=sink,
            outputs={},
            warnings=[],
        )

    assert sink.calls == 1
    assert len(opened) == 2
    assert all(rows._closed for rows in opened)
    assert not child_temp_dir.exists()


def test_lifecycle_edge2_raises_after_edge1_registered(tmp_path: Path, monkeypatch: Any) -> None:
    plan, sources, edge_a, edge_b = _two_edge_setup(
        OrphanPolicy.PRESERVE, OrphanPolicy.PRESERVE, n=12
    )
    parent_relations = _build_parent_relations(plan, sources, edge_a, edge_b, tmp_path / "parents")

    call_count = [0]
    opened: list[Any] = []
    orig = StreamFkJoiner.run_ordered_join

    def spy(self: StreamFkJoiner, *args: Any, **kwargs: Any) -> Any:
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("boom-edge-2")
        rows = orig(self, *args, **kwargs)
        opened.append(rows)
        return rows

    monkeypatch.setattr(StreamFkJoiner, "run_ordered_join", spy)
    sink = _RecordingSink()
    child_temp_dir = tmp_path / "joins" / "child"

    with pytest.raises(RuntimeError, match="boom-edge-2"):
        stream_table(
            plan,
            "child",
            sources["child"],
            incoming_edges=(edge_a, edge_b),
            outgoing_edges=(),
            parent_relations=parent_relations,
            temp_dir=child_temp_dir,
            relation_dir=tmp_path / "relations" / "child",
            staging_path=tmp_path / "staged" / "child" / "masked_keys.parquet",
            memory_limit=None,
            batch_rows=None,
            run_bytes_cap=_RUN_BYTES_CAP,
            sink=sink,
            outputs={},
            warnings=[],
        )

    assert sink.calls == 0
    assert len(opened) == 1  # only edge_a's cursor ever entered the ExitStack
    assert opened[0]._closed is True
    assert not child_temp_dir.exists()


# ---------------------------------------------------------------------------
# #6: FAIL-precount lifecycle -- at most one live DuckDB connection at a
# time, plus the later-edge-orphan variant.
# ---------------------------------------------------------------------------


def test_fail_precount_zero_orphan_edges_one_connection_at_a_time(
    tmp_path: Path, monkeypatch: Any
) -> None:
    plan, sources, edge_a, edge_b = _two_edge_setup(OrphanPolicy.FAIL, OrphanPolicy.FAIL, n=8)
    parent_relations = _build_parent_relations(plan, sources, edge_a, edge_b, tmp_path / "parents")

    live = [0]
    max_live = [0]
    orig_ensure = StreamFkJoiner._ensure_conn

    def spy_ensure(self: StreamFkJoiner) -> Any:
        was_open = self._conn is not None
        conn = orig_ensure(self)
        if not was_open:
            live[0] += 1
            max_live[0] = max(max_live[0], live[0])
        return conn

    monkeypatch.setattr(StreamFkJoiner, "_ensure_conn", spy_ensure)
    orig_close = StreamFkJoiner.close

    def spy_close(self: StreamFkJoiner) -> None:
        if self._conn is not None:
            live[0] -= 1
        orig_close(self)

    monkeypatch.setattr(StreamFkJoiner, "close", spy_close)

    total_orphans_calls = [0]
    orig_total = StreamFkJoiner.total_orphans

    def spy_total(self: StreamFkJoiner) -> int:
        total_orphans_calls[0] += 1
        return orig_total(self)

    monkeypatch.setattr(StreamFkJoiner, "total_orphans", spy_total)
    ordered_calls = [0]
    orig_ordered = StreamFkJoiner.run_ordered_join

    def spy_ordered(self: StreamFkJoiner, *args: Any, **kwargs: Any) -> Any:
        ordered_calls[0] += 1
        return orig_ordered(self, *args, **kwargs)

    monkeypatch.setattr(StreamFkJoiner, "run_ordered_join", spy_ordered)

    outputs: dict[str, pa.Table] = {}
    stream_table(
        plan,
        "child",
        sources["child"],
        incoming_edges=(edge_a, edge_b),
        outgoing_edges=(),
        parent_relations=parent_relations,
        temp_dir=tmp_path / "joins" / "child",
        relation_dir=tmp_path / "relations" / "child",
        staging_path=tmp_path / "staged" / "child" / "masked_keys.parquet",
        memory_limit=None,
        batch_rows=None,
        run_bytes_cap=_RUN_BYTES_CAP,
        sink=None,
        outputs=outputs,
        warnings=[],
    )

    assert total_orphans_calls[0] == 2
    assert ordered_calls[0] == 2
    assert max_live[0] == 1, f"more than one live DuckDB connection at once: peak={max_live[0]}"
    assert outputs["child"].num_rows == 8


def test_fail_precount_later_edge_orphan_raises_and_cleans_edge1(
    tmp_path: Path, monkeypatch: Any
) -> None:
    plan, sources, edge_a, edge_b = _two_edge_setup(
        OrphanPolicy.FAIL, OrphanPolicy.FAIL, n=8, orphan_in_edge2=True
    )
    parent_relations = _build_parent_relations(plan, sources, edge_a, edge_b, tmp_path / "parents")
    opened = _spy_ordered_join(monkeypatch)
    sink = _RecordingSink()

    with pytest.raises(ExecutionError) as excinfo:
        stream_table(
            plan,
            "child",
            sources["child"],
            incoming_edges=(edge_a, edge_b),
            outgoing_edges=(),
            parent_relations=parent_relations,
            temp_dir=tmp_path / "joins" / "child",
            relation_dir=tmp_path / "relations" / "child",
            staging_path=tmp_path / "staged" / "child" / "masked_keys.parquet",
            memory_limit=None,
            batch_rows=None,
            run_bytes_cap=_RUN_BYTES_CAP,
            sink=sink,
            outputs={},
            warnings=[],
        )

    assert excinfo.value.code == "orphan_fk_violation"
    assert "1 orphan" in excinfo.value.message
    assert sink.calls == 0
    # edge_a completed its ordered join and entered the ExitStack before
    # edge_b's FAIL precount raised; only edge_a's cursor was ever opened.
    assert len(opened) == 1
    assert opened[0]._closed is True


# ---------------------------------------------------------------------------
# #7b: single-read `raw_parent_source` forwarding, sink and resident paths.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CountingReversingSource(LazySource):
    """A `LazySource` whose first read is faithful and every later read
    returns the rows REVERSED -- the same-count permutation
    `RawParentKeySpill` exists to make harmless. `calls` is a one-element
    list used as a mutable counter cell (the dataclass itself stays frozen)."""

    calls: list[int] = field(default_factory=lambda: [0])

    def iter_batches(self, batch_rows: int) -> Any:
        self.calls[0] += 1
        if self.calls[0] == 1:
            yield from super().iter_batches(batch_rows)
            return
        table = self.to_table()
        n = table.num_rows
        reversed_table = table.take(pa.array(list(range(n - 1, -1, -1)), type=pa.int64()))
        yield from reversed_table.to_batches(max_chunksize=batch_rows)


def _write_chain_child_as_parquet(child: pa.Table, path: Path) -> _CountingReversingSource:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(child, path)
    return _CountingReversingSource(path=path)


@pytest.mark.parametrize("use_sink", [False, True], ids=["resident", "sink"])
def test_single_read_raw_parent_source_forwarding(tmp_path: Path, use_sink: bool) -> None:
    """parent -> child -> grandchild; `child` is both an incoming-edge child
    (of parent) and an outgoing-edge parent (of grandchild). If phase 1
    forwarded a SECOND read of child's source into the outgoing-relation
    build instead of the captured `RawParentKeySpill`, the spy's reversed
    second read would corrupt grandchild's resolved values."""
    plan, sources, graph = _build_chain(
        strategy="passthrough",
        policy=OrphanPolicy.PRESERVE,
        child_refs=[0, 1, 2, 0, 3],
        gc_refs=[0, 1, 2, 3, 4, 0],
    )
    oracle = _run_oracle(plan, sources, graph)

    child_path = tmp_path / f"child-{use_sink}.parquet"
    spy_child = _write_chain_child_as_parquet(sources["child"], child_path)
    lazy_sources = dict(sources)
    lazy_sources["child"] = spy_child

    if use_sink:
        target = tmp_path / "published"
        run_stream_driver(
            plan,
            lazy_sources,
            graph,
            temp_dir=tmp_path / "work-sink",
            sink=ParquetTransactionalSink(target),
        )
        grandchild = pq.read_table(target / "grandchild.parquet")
    else:
        res = run_stream_driver(plan, lazy_sources, graph, temp_dir=tmp_path / "work-mem")
        grandchild = res.outputs["grandchild"]

    assert spy_child.calls[0] == 1, (
        f"child source read {spy_child.calls[0]} time(s), expected exactly 1"
    )
    _assert_value_equal(oracle.outputs["grandchild"], grandchild, f"single-read/{use_sink}")


__all__: list[str] = []
