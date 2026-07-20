"""SPRINT-1 Part B wiring: `run_out_of_core_route` runs the hybrid memory
preflight BEFORE dispatching to `run_fk_out_of_core`, gated against the EXACT
`budget_bytes` `resolve_ooc_memory_limit` resolves at the route's own call
site (`_parent_table_row_counts` / `_incoming_edge_counts` feed the per-table
floor/cap the preflight needs) -- the fix for the BLOCKER where a preflight
compared against a fraction of the raw ceiling instead of the real,
phase-aware cap Part A actually hands DuckDB.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline_route_exec import (
    _incoming_edge_counts,
    _parent_table_row_counts,
)
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph


def _edge(parent: str, child: str) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent,
        parent_columns=("id",),
        child_table=child,
        child_columns=(f"{parent}_id",),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def _graph(*edges: RelationshipEdge) -> RelationshipGraph:
    return RelationshipGraph(edges=tuple(edges), ordering=())


class TestParentTableRowCounts:
    def test_no_edges_yields_empty(self) -> None:
        assert _parent_table_row_counts({}, _graph()) == {}

    def test_prices_every_parent_across_outgoing_edges(self) -> None:
        graph = _graph(_edge("parent", "child"), _edge("child", "grandchild"))
        sources = {
            "parent": pa.table({"id": range(10)}),
            "child": pa.table({"id": range(1_000)}),
            "grandchild": pa.table({"id": range(5)}),
        }
        # "parent" and "child" are both parent-side (10 and 1,000 rows);
        # "grandchild" is never a parent, so it is not priced at all.
        assert _parent_table_row_counts(sources, graph) == {"parent": 10, "child": 1_000}

    def test_missing_source_fails_closed_instead_of_under_counting(self) -> None:
        # LOW remediation: a graph parent table absent from `sources` must
        # NOT silently contribute 0 rows (an under-count admits a job the
        # preflight should have refused) -- it must fail closed instead.
        graph = _graph(_edge("parent", "child"))
        with pytest.raises(ExecutionError) as excinfo:
            _parent_table_row_counts({}, graph)
        assert excinfo.value.code == "out_of_core_parent_rows_unresolved"

    def test_reads_lazy_source_row_count_without_materializing(self, tmp_path) -> None:
        path = tmp_path / "parent.parquet"
        pa.parquet.write_table(pa.table({"id": range(42)}), path)
        graph = _graph(_edge("parent", "child"))
        sources = {"parent": LazySource(path=path), "child": pa.table({"id": range(5)})}
        assert _parent_table_row_counts(sources, graph) == {"parent": 42}


class TestIncomingEdgeCounts:
    def test_no_edges_yields_empty(self) -> None:
        assert _incoming_edge_counts(_graph()) == {}

    def test_counts_fan_in_per_child_table(self) -> None:
        graph = _graph(_edge("parent_a", "hub"), _edge("parent_b", "hub"), _edge("hub", "leaf"))
        assert _incoming_edge_counts(graph) == {"hub": 2, "leaf": 1}


class TestMemoryPreflightWiring:
    def test_hard_fail_raises_before_the_ooc_runner_is_ever_called(
        self, tmp_path, monkeypatch
    ) -> None:
        from decoy_engine.execution import _pipeline_route_exec as route_exec_mod
        from decoy_engine.execution import out_of_core as ooc_pkg

        called = {"run_fk_out_of_core": False}

        def spy(*args, **kwargs):
            called["run_fk_out_of_core"] = True
            raise AssertionError("run_fk_out_of_core must not run past a hard-fail preflight")

        monkeypatch.setattr(ooc_pkg, "run_fk_out_of_core", spy)

        graph = _graph(_edge("parent", "child"))
        # A large parent table under a tiny EXPLICIT budget: `resolve_ooc_
        # memory_limit(budget_bytes=...)` skips host-RAM detection entirely
        # (LOW remediation, FIX 5), so this is deterministic regardless of the
        # real host's memory -- no monkeypatching of memory detection needed.
        sources = {
            "parent": pa.table({"id": [f"p{i}" for i in range(20_000_000)]}),
            "child": pa.table({"parent_id": ["p1"]}),
        }

        with pytest.raises(ExecutionError) as excinfo:
            route_exec_mod.run_out_of_core_route(
                plan=object(),
                sources=sources,
                registry=object(),
                graph=graph,
                sink=None,
                route_reason="test",
                table_kinds={},
                source_loader=None,
                sources_resident=True,
                budget_bytes=64 * 1024 * 1024,  # 64 MiB: far below any real floor
            )
        assert excinfo.value.code == "out_of_core_insufficient_memory"
        assert called["run_fk_out_of_core"] is False

    def test_admits_a_job_whose_floor_fits_its_actual_cap(self, tmp_path, monkeypatch) -> None:
        from decoy_engine.execution import _pipeline_route_exec as route_exec_mod

        graph = _graph(_edge("parent", "child"))
        sources = {
            "parent": pa.table({"id": ["p1", "p2"]}),
            "child": pa.table({"parent_id": pa.array([], type=pa.string())}),
        }

        result = route_exec_mod.run_out_of_core_route(
            plan=_tiny_plan(),
            sources=sources,
            registry=_tiny_registry(),
            graph=graph,
            sink=None,
            route_reason="test",
            table_kinds={},
            source_loader=None,
            sources_resident=True,
            # Ample explicit budget for a 2-row parent table: the preflight
            # must not refuse a job whose floor genuinely fits its cap.
            budget_bytes=512 * 1024 * 1024,
        )
        assert result.outputs["parent"].num_rows == 2


def _tiny_plan():
    """A minimal parent(id) -> child(parent_id) plan: `child`'s FK column is
    seeded under the SAME namespace as `parent`'s key column (mirrors
    `test_out_of_core_runner_phase_caps.py`'s fixture -- the namespace is how
    the out-of-core join resolves a child's FK column against its parent
    key, so both sides need a `ColumnSeed` even though the FK column's own
    value is join-produced, not independently masked)."""
    from types import SimpleNamespace

    from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed

    col = ColumnSeed(
        namespace="ns",
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x00" * 8,
            per_table=(
                ("parent", TableSeed(per_column=(("id", col),), per_group=())),
                ("child", TableSeed(per_column=(("parent_id", col),), per_group=())),
            ),
        )
    )


def _tiny_registry():
    from decoy_engine.providers_v2 import get_default_registry

    return get_default_registry()
