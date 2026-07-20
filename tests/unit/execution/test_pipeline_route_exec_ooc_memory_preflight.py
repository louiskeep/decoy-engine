"""SPRINT-1 Part B wiring: `run_out_of_core_route` runs the hybrid memory
preflight BEFORE dispatching to `run_fk_out_of_core`, using per-table row
counts it already has in hand (`_max_parent_table_rows`) -- the same
"site with row counts already in hand" precedent the disk preflight
(`enforce_ooc_disk_preflight`) establishes at its own call site.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline_route_exec import _max_parent_table_rows
from decoy_engine.execution.out_of_core import _memory_estimate as mem_mod
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


class TestMaxParentTableRows:
    def test_no_edges_yields_zero(self) -> None:
        assert _max_parent_table_rows({}, _graph()) == 0

    def test_prices_the_largest_parent_across_outgoing_edges(self) -> None:
        graph = _graph(_edge("parent", "child"), _edge("child", "grandchild"))
        sources = {
            "parent": pa.table({"id": range(10)}),
            "child": pa.table({"id": range(1_000)}),
            "grandchild": pa.table({"id": range(5)}),
        }
        # "parent" and "child" are both parent-side (10 and 1,000 rows);
        # "grandchild" is never a parent, so its row count is irrelevant.
        assert _max_parent_table_rows(sources, graph) == 1_000

    def test_missing_source_contributes_nothing(self) -> None:
        graph = _graph(_edge("parent", "child"))
        assert _max_parent_table_rows({}, graph) == 0

    def test_reads_lazy_source_row_count_without_materializing(self, tmp_path) -> None:
        path = tmp_path / "parent.parquet"
        pa.parquet.write_table(pa.table({"id": range(42)}), path)
        graph = _graph(_edge("parent", "child"))
        sources = {"parent": LazySource(path=path), "child": pa.table({"id": range(5)})}
        assert _max_parent_table_rows(sources, graph) == 42


class TestMemoryPreflightWiring:
    def test_hard_fail_raises_before_the_ooc_runner_is_ever_called(
        self, tmp_path, monkeypatch
    ) -> None:
        from decoy_engine.execution import _pipeline_route_exec as route_exec_mod
        from decoy_engine.execution import out_of_core as ooc_pkg

        # A tiny detected ceiling puts even the fixed baseline floor (~128
        # MiB) far past the safe bound, forcing a hard-fail regardless of
        # table size.
        monkeypatch.setattr(mem_mod, "detect_effective_memory_bytes", lambda: 1 * 1024 * 1024)

        called = {"run_fk_out_of_core": False}

        def spy(*args, **kwargs):
            called["run_fk_out_of_core"] = True
            raise AssertionError("run_fk_out_of_core must not run past a hard-fail preflight")

        monkeypatch.setattr(ooc_pkg, "run_fk_out_of_core", spy)

        graph = _graph(_edge("parent", "child"))
        sources = {"parent": pa.table({"id": ["p1"]}), "child": pa.table({"parent_id": ["p1"]})}

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
            )
        assert excinfo.value.code == "out_of_core_insufficient_memory"
        assert called["run_fk_out_of_core"] is False
