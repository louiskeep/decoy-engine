"""Multi-parent FK support (capability-gaps WS5, 2026-06-12).

The same child column-tuple may declare FK relationships to MULTIPLE
parent tables (polymorphic/shared-domain keys). Semantics:

- a child value resolves against each parent's source->masked map in
  DECLARED CONFIG ORDER; first hit wins (deterministic, documented);
- a value is an orphan only when it is absent from ALL parent maps;
- the per-edge orphan policies on one shared child tuple must be
  identical (combining remap with drop has no coherent meaning) --
  conflict raises `orphan_policy_conflict` at graph build.

Replaces the S2-era `multi_parent_fk_unsupported` rejection.
"""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa
import pytest

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Profile, Relationship
from decoy_engine.relationships import (
    build_namespace_registry,
    build_relationship_graph,
)
from decoy_engine.relationships._graph import OrphanPolicy


def _rels(ns_a: str = "shared_ns", ns_b: str = "shared_ns") -> tuple[Relationship, ...]:
    return (
        Relationship(
            parent_table="parent_a",
            parent_columns=("id",),
            child_table="child",
            child_columns=("id",),
            namespace=ns_a,
        ),
        Relationship(
            parent_table="parent_b",
            parent_columns=("id",),
            child_table="child",
            child_columns=("id",),
            namespace=ns_b,
        ),
    )


def _graph(rels, *, policy_a: str = "preserve", policy_b: str = "preserve"):
    profile = Profile(
        schema_version=1,
        tables=(),
        relationships=rels,
        profiled_at=datetime(2026, 6, 12, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )
    registry = build_namespace_registry({"tables": []}, profile)
    lookup = {
        ("parent_a", ("id",), "child", ("id",)): OrphanPolicy(policy_a),
        ("parent_b", ("id",), "child", ("id",)): OrphanPolicy(policy_b),
    }
    return build_relationship_graph(
        profile.relationships, namespace_registry=registry, orphan_policy_lookup=lookup
    )


class TestGraphAcceptsMultiParent:
    def test_two_parents_one_child_builds(self) -> None:
        graph = _graph(_rels())
        edges = graph.parents_of("child", ("id",))
        assert len(edges) == 2

    def test_declared_order_preserved(self) -> None:
        """First hit wins at resolve time, so edge order IS semantics."""
        graph = _graph(_rels())
        edges = graph.parents_of("child", ("id",))
        assert [e.parent_table for e in edges] == ["parent_a", "parent_b"]

    def test_ordering_places_both_parents_before_child(self) -> None:
        graph = _graph(_rels())
        order = [t for t, _ in graph.ordering]
        assert order.index("parent_a") < order.index("child")
        assert order.index("parent_b") < order.index("child")


class TestOrphanPolicyConflict:
    def test_conflicting_policies_raise(self) -> None:
        with pytest.raises(PlanCompileError) as exc:
            _graph(_rels(), policy_a="remap", policy_b="fail")
        assert exc.value.code == "orphan_policy_conflict"
        assert "remap" in str(exc.value) and "fail" in str(exc.value)

    def test_identical_policies_pass(self) -> None:
        graph = _graph(_rels(), policy_a="remap", policy_b="remap")
        assert len(graph.parents_of("child", ("id",))) == 2


class TestMultiParentResolveE2E:
    """Through the pandas adapter (the established orphan-FK test shape):
    child values map through whichever parent holds them; overlapping
    keys resolve to the FIRST declared parent; a key in neither parent
    is the orphan."""

    @staticmethod
    def _col(strategy: str, namespace: str, provider_config=()) -> object:
        from decoy_engine.plan._types import ColumnSeed

        return ColumnSeed(
            namespace=namespace,
            strategy=strategy,
            provider=strategy,
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=True,
            provider_config=provider_config,
            coherent_with=(),
        )

    def _run(self, *, policy: str = "preserve"):
        from types import SimpleNamespace

        from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
        from decoy_engine.plan._types import SeedEnvelope, TableSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import (
            RelationshipEdge,
            RelationshipGraph,
        )
        from decoy_engine.relationships._namespace import NamespaceRegistry

        seed = (0xBEEF).to_bytes(8, "big")
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=seed,
                per_table=(
                    (
                        "parent_a",
                        TableSeed(per_column=(("id", self._col("hash", "ns_a")),), per_group=()),
                    ),
                    (
                        "parent_b",
                        TableSeed(
                            per_column=(
                                (
                                    "id",
                                    self._col(
                                        "fpe", "ns_b", (("charset", "alphanum"),)
                                    ),
                                ),
                            ),
                            per_group=(),
                        ),
                    ),
                    (
                        "child",
                        TableSeed(per_column=(("id", self._col("hash", "ns_a")),), per_group=()),
                    ),
                ),
            )
        )
        edges = tuple(
            RelationshipEdge(
                parent_table=parent,
                parent_columns=("id",),
                child_table="child",
                child_columns=("id",),
                namespace=ns,
                orphan_policy=OrphanPolicy(policy),
            )
            for parent, ns in (("parent_a", "ns_a"), ("parent_b", "ns_b"))
        )
        graph = RelationshipGraph(edges=edges, ordering=())
        sources = {
            # "shared1" lives in BOTH parents: precedence cell.
            "parent_a": pa.table({"id": ["a1", "a2", "shared1"]}),
            "parent_b": pa.table({"id": ["b1", "b2", "shared1"]}),
            "child": pa.table({"id": ["a1", "b1", "shared1", "ghost"]}),
        }
        return PandasExecutionAdapter().run(
            plan,
            sources,
            registry=get_default_registry(),
            relationship_graph=graph,
            namespace_registry=NamespaceRegistry(bindings=()),
        )

    def test_child_resolves_through_both_parents(self) -> None:
        result = self._run()
        out_a = result.outputs["parent_a"].column("id").to_pylist()
        out_b = result.outputs["parent_b"].column("id").to_pylist()
        child = result.outputs["child"].column("id").to_pylist()
        assert child[0] == out_a[0]  # a1 through parent_a's mask
        assert child[1] == out_b[0]  # b1 through parent_b's mask

    def test_overlapping_key_resolves_to_first_declared_parent(self) -> None:
        result = self._run()
        out_a = result.outputs["parent_a"].column("id").to_pylist()
        out_b = result.outputs["parent_b"].column("id").to_pylist()
        child = result.outputs["child"].column("id").to_pylist()
        # "shared1" is in both parents with DIFFERENT masked values
        # (hash under ns_a vs fpe under ns_b); declared order wins.
        assert out_a[2] != out_b[2]
        assert child[2] == out_a[2]

    def test_orphan_only_when_absent_from_all_parents(self) -> None:
        result = self._run(policy="preserve")
        child = result.outputs["child"].column("id").to_pylist()
        assert child[3] == "ghost"  # in neither parent: preserved orphan

    def test_orphan_fail_policy_raises(self) -> None:
        from decoy_engine.execution import ExecutionError

        with pytest.raises(ExecutionError) as exc:
            self._run(policy="fail")
        assert exc.value.code == "orphan_fk_violation"

    def test_orphan_warn_aggregates_once(self) -> None:
        result = self._run(policy="warn")
        orphan_warnings = [w for w in result.warnings if w.code == "orphan_fk"]
        assert len(orphan_warnings) == 1
        assert orphan_warnings[0].detail["orphan_rows"] == 1

    def test_deterministic(self) -> None:
        r1 = self._run()
        r2 = self._run()
        assert r1.outputs["child"].equals(r2.outputs["child"])
