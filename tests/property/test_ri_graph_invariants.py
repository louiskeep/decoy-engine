"""Property + metamorphic invariants for the RI/FK relationship graph.

TQ-0 pilot (Test-Quality Program, 2026-07-25). The existing
`tests/unit/relationships/` suite is example-based and already gives
`_graph.py` 91% line/branch coverage, but coverage does not prove the tests
would CATCH a bug: a mutation-graded suite needs oracles independent of the
implementation. These are those oracles for the referential-integrity graph,
the worst-blast-radius invariant in the engine (a wrong mask order or a
dropped edge silently corrupts FK integrity in the output).

The invariants come from `relationships/_graph.py`'s own contract (module
docstring + `build_relationship_graph` docstring) and the engine-v2 S2 spec:

- ORDERING respects every edge: a parent node precedes its child node, so a
  child never masks before the parent whose keys it must match.
- DETERMINISM / purity: equal input produces an equal graph, and input ORDER
  never changes the output (Kahn + heapq lexicographic tie-break is byte-stable
  across runs). This is the metamorphic relation that makes cross-process
  golden fingerprints stable.
- DEDUP idempotence: an exactly-duplicated relationship does not change the
  graph (audit M1: duplicate edges used to inflate indegree bookkeeping).
- CYCLE rejection: a cyclic FK graph has no deterministic mask order, so it is
  refused rather than silently mis-ordered.
- COMPLETENESS round-trip: `check_orphan_fk_policy_completeness` returns a
  lookup that `build_relationship_graph` accepts without an
  `orphan_fk_policy_missing`; the two functions compose.

Run:  pytest tests/property/test_ri_graph_invariants.py -q
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Relationship
from decoy_engine.relationships._graph import (
    OrphanPolicy,
    build_relationship_graph,
    check_orphan_fk_policy_completeness,
)
from decoy_engine.relationships._namespace import NamespaceRegistry

# Match the existing property suite's audit profile: more examples than the
# 100-example default, no deadline (graph builds are cheap but Hypothesis
# shrinking can trip the 200ms wall), and print_blob so any counterexample is
# replayable.
settings.register_profile(
    "audit",
    max_examples=300,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("audit")

# `for_relationship` returns a Relationship's own `namespace` when set, so an
# EMPTY registry resolves every edge whose relationship carries one (see
# `_namespace.for_relationship`). This keeps the strategies focused on the
# graph invariants rather than namespace-registry wiring, which has its own
# tests.
_EMPTY_REGISTRY = NamespaceRegistry(bindings=())


@st.composite
def dag(draw: st.DrawFn) -> tuple[tuple[Relationship, ...], dict]:
    """A random ACYCLIC set of FK relationships plus a complete orphan-policy
    lookup for it.

    Acyclic BY CONSTRUCTION: tables are indexed `t0..t{n-1}` and every edge
    goes from a lower index to a higher one, so the node graph can never form
    a cycle. Each edge's child column is unique to its parent, so two edges
    into the same child table land on DIFFERENT child nodes and never trip the
    multi-parent orphan-policy-conflict guard (that guard has its own test).
    """
    n_tables = draw(st.integers(min_value=1, max_value=6))
    tables = [f"t{i}" for i in range(n_tables)]
    candidates = [(i, j) for i in range(n_tables) for j in range(i + 1, n_tables)]
    chosen = (
        draw(st.lists(st.sampled_from(candidates), unique=True, max_size=len(candidates)))
        if candidates
        else []
    )
    rels: list[Relationship] = []
    lookup: dict = {}
    for i, j in chosen:
        pt, ct = tables[i], tables[j]
        pc = ("id",)
        cc = (f"{pt}_fk",)
        rels.append(
            Relationship(
                parent_table=pt,
                parent_columns=pc,
                child_table=ct,
                child_columns=cc,
                namespace=f"ns_{pt}_{ct}",
            )
        )
        lookup[(pt, pc, ct, cc)] = draw(st.sampled_from(list(OrphanPolicy)))
    return tuple(rels), lookup


def _build(rels: tuple[Relationship, ...], lookup: dict):
    return build_relationship_graph(
        rels, namespace_registry=_EMPTY_REGISTRY, orphan_policy_lookup=lookup
    )


def _index(ordering: tuple, node: tuple) -> int:
    return ordering.index(node)


# --------------------------------------------------------------------------
# build_relationship_graph invariants
# --------------------------------------------------------------------------


@given(dag())
def test_ordering_respects_every_edge(data) -> None:
    """The RI-critical invariant: every parent node precedes its child node,
    so no child masks before the parent whose keys it references."""
    rels, lookup = data
    graph = _build(rels, lookup)
    for e in graph.edges:
        parent_node = (e.parent_table, e.parent_columns)
        child_node = (e.child_table, e.child_columns)
        assert _index(graph.ordering, parent_node) < _index(graph.ordering, child_node)


@given(dag())
def test_build_is_deterministic(data) -> None:
    """Equal input produces an equal graph (pure function)."""
    rels, lookup = data
    first = _build(rels, lookup)
    second = _build(rels, lookup)
    assert first.edges == second.edges
    assert first.ordering == second.ordering


@given(dag(), st.randoms(use_true_random=False))
def test_input_order_does_not_change_output(data, rng) -> None:
    """Metamorphic: shuffling the input relationships leaves both `edges` and
    `ordering` byte-identical. This is what keeps cross-process golden
    fingerprints stable regardless of config iteration order."""
    rels, lookup = data
    shuffled = list(rels)
    rng.shuffle(shuffled)
    base = _build(rels, lookup)
    perm = _build(tuple(shuffled), lookup)
    assert base.edges == perm.edges
    assert base.ordering == perm.ordering


@given(dag())
def test_ordering_is_a_permutation_of_the_node_set(data) -> None:
    """Every node appears in `ordering` exactly once, and nothing else does."""
    rels, lookup = data
    graph = _build(rels, lookup)
    nodes = set()
    for e in graph.edges:
        nodes.add((e.parent_table, e.parent_columns))
        nodes.add((e.child_table, e.child_columns))
    assert len(graph.ordering) == len(nodes)
    assert set(graph.ordering) == nodes


@given(dag(), st.integers(min_value=0, max_value=5))
def test_duplicate_relationship_is_idempotent(data, dup_index) -> None:
    """Metamorphic: appending an exact-duplicate relationship does not change
    the graph (audit M1: duplicates once inflated indegree bookkeeping)."""
    rels, lookup = data
    assume(len(rels) > 0)
    dup = rels[dup_index % len(rels)]
    with_dup = (*rels, dup)
    base = _build(rels, lookup)
    doubled = _build(with_dup, lookup)
    assert base.edges == doubled.edges
    assert base.ordering == doubled.ordering


@given(dag())
def test_parents_and_children_roundtrip(data) -> None:
    """Every edge is discoverable from both ends via the query helpers."""
    rels, lookup = data
    graph = _build(rels, lookup)
    for e in graph.edges:
        assert e in graph.parents_of(e.child_table, e.child_columns)
        assert e in graph.children_of(e.parent_table, e.parent_columns)


def test_cycle_is_rejected() -> None:
    """A cyclic FK graph has no deterministic mask order and is refused."""
    rels = (
        Relationship(
            parent_table="A",
            parent_columns=("k",),
            child_table="B",
            child_columns=("k",),
            namespace="ns",
        ),
        Relationship(
            parent_table="B",
            parent_columns=("k",),
            child_table="A",
            child_columns=("k",),
            namespace="ns",
        ),
    )
    lookup = {
        ("A", ("k",), "B", ("k",)): OrphanPolicy.PRESERVE,
        ("B", ("k",), "A", ("k",)): OrphanPolicy.PRESERVE,
    }
    with pytest.raises(PlanCompileError) as ei:
        _build(rels, lookup)
    assert ei.value.code == "fk_cycle"


def test_composite_parent_collapses_to_one_node() -> None:
    """A composite-key parent is one node (the whole column tuple), so a child
    waits on the whole parent tuple, not on individual columns."""
    rels = (
        Relationship(
            parent_table="orders",
            parent_columns=("region", "order_no"),
            child_table="lines",
            child_columns=("region", "order_no"),
            namespace="ns",
        ),
    )
    lookup = {("orders", ("region", "order_no"), "lines", ("region", "order_no")): OrphanPolicy.FAIL}
    graph = _build(rels, lookup)
    assert ("orders", ("region", "order_no")) in graph.ordering
    assert _index(graph.ordering, ("orders", ("region", "order_no"))) < _index(
        graph.ordering, ("lines", ("region", "order_no"))
    )


def test_multi_parent_conflicting_policy_is_rejected() -> None:
    """A child tuple referencing two parents with DIFFERENT orphan policies is
    a declaration error (the executor resolves through every parent before a
    row is an orphan, so one policy must govern)."""
    rels = (
        Relationship("p1", ("id",), "c", ("pid",), namespace="ns"),
        Relationship("p2", ("id",), "c", ("pid",), namespace="ns"),
    )
    lookup = {
        ("p1", ("id",), "c", ("pid",)): OrphanPolicy.PRESERVE,
        ("p2", ("id",), "c", ("pid",)): OrphanPolicy.FAIL,
    }
    with pytest.raises(PlanCompileError) as ei:
        _build(rels, lookup)
    assert ei.value.code == "orphan_policy_conflict"


# --------------------------------------------------------------------------
# check_orphan_fk_policy_completeness invariants
# --------------------------------------------------------------------------


def _config_for(rels: tuple[Relationship, ...], policies: dict[tuple, str]) -> dict:
    """A minimal config `relationships:` block declaring one orphan_policy per
    (parent, child) end, in the shape the check parses."""
    entries = []
    for r in rels:
        entries.append(
            {
                "parent": {"table": r.parent_table, "columns": list(r.parent_columns)},
                "children": [{"table": r.child_table, "columns": list(r.child_columns)}],
                "orphan_policy": policies[
                    (r.parent_table, r.parent_columns, r.child_table, r.child_columns)
                ],
            }
        )
    return {"relationships": entries}


@given(dag())
def test_completeness_roundtrip_composes_with_build(data) -> None:
    """Metamorphic composition: a config that declares a policy for every
    relationship yields a lookup `build_relationship_graph` accepts without an
    `orphan_fk_policy_missing`. The two functions are each other's contract."""
    rels, lookup = data
    policies = {k: v.value for k, v in lookup.items()}
    config = _config_for(rels, policies)
    resolved = check_orphan_fk_policy_completeness(config, rels)
    assert set(resolved) == set(k for k in lookup)
    # The resolved lookup must build without the wiring-bug error path.
    _build(rels, resolved)


def test_missing_policy_field_is_rejected() -> None:
    rels = (Relationship("p", ("id",), "c", ("pid",), namespace="ns"),)
    config = {
        "relationships": [
            {"parent": {"table": "p", "columns": ["id"]}, "children": [{"table": "c", "columns": ["pid"]}]}
        ]
    }
    with pytest.raises(PlanCompileError) as ei:
        check_orphan_fk_policy_completeness(config, rels)
    assert ei.value.code == "orphan_fk_policy_missing"


def test_invalid_policy_value_is_rejected() -> None:
    rels = (Relationship("p", ("id",), "c", ("pid",), namespace="ns"),)
    config = _config_for(rels, {("p", ("id",), "c", ("pid",)): "sometimes"})
    with pytest.raises(PlanCompileError) as ei:
        check_orphan_fk_policy_completeness(config, rels)
    assert ei.value.code == "orphan_fk_policy_invalid"


def test_relationship_absent_from_config_is_rejected() -> None:
    rels = (Relationship("p", ("id",), "c", ("pid",), namespace="ns"),)
    with pytest.raises(PlanCompileError) as ei:
        check_orphan_fk_policy_completeness({"relationships": []}, rels)
    assert ei.value.code == "orphan_fk_policy_missing"


def test_duplicate_conflicting_policy_is_rejected() -> None:
    """Same (parent, child) declared twice with DIFFERENT policies: a silent
    last-wins overwrite used to hide the conflict (QA-8 F3)."""
    rels = (Relationship("p", ("id",), "c", ("pid",), namespace="ns"),)
    config = {
        "relationships": [
            {
                "parent": {"table": "p", "columns": ["id"]},
                "children": [{"table": "c", "columns": ["pid"]}],
                "orphan_policy": "preserve",
            },
            {
                "parent": {"table": "p", "columns": ["id"]},
                "children": [{"table": "c", "columns": ["pid"]}],
                "orphan_policy": "fail",
            },
        ]
    }
    with pytest.raises(PlanCompileError) as ei:
        check_orphan_fk_policy_completeness(config, rels)
    assert ei.value.code == "orphan_fk_policy_duplicate"


def test_build_raises_on_lookup_key_missing() -> None:
    """The wiring-bug guard: `build_relationship_graph` called with a
    relationship absent from the orphan-policy lookup (i.e.
    `check_orphan_fk_policy_completeness` was not run first) fails loudly
    rather than emitting an edge with no policy."""
    rels = (Relationship("p", ("id",), "c", ("pid",), namespace="ns"),)
    with pytest.raises(PlanCompileError) as ei:
        _build(rels, {})  # empty lookup: the key is missing
    assert ei.value.code == "orphan_fk_policy_missing"


@pytest.mark.parametrize(
    "config",
    [
        {"relationships": "not-a-list"},
        {"relationships": ["not-a-dict"]},
        {"relationships": [{"parent": "not-a-dict", "orphan_policy": "preserve"}]},
        {"relationships": [{"parent": {"columns": ["id"]}, "orphan_policy": "preserve"}]},
        {"relationships": [{"parent": {"table": "p", "columns": "not-a-list"}, "orphan_policy": "preserve"}]},
        {
            "relationships": [
                {
                    "parent": {"table": "p", "columns": ["id"]},
                    "orphan_policy": "preserve",
                    "children": "not-a-list",
                }
            ]
        },
        {
            "relationships": [
                {
                    "parent": {"table": "p", "columns": ["id"]},
                    "orphan_policy": "preserve",
                    "children": ["not-a-dict"],
                }
            ]
        },
        {
            "relationships": [
                {
                    "parent": {"table": "p", "columns": ["id"]},
                    "orphan_policy": "preserve",
                    "children": [{"columns": ["pid"]}],
                }
            ]
        },
    ],
    ids=[
        "relationships-not-list",
        "entry-not-dict",
        "parent-not-dict",
        "parent-missing-table",
        "parent-columns-not-list",
        "children-not-list",
        "child-not-dict",
        "child-missing-table",
    ],
)
def test_malformed_config_entry_is_skipped_leaving_relationship_unresolved(config) -> None:
    """A structurally malformed config entry is skipped, not crashed on. The
    real relationship then has no matching entry, so completeness reports it
    missing (the honest outcome: a malformed entry does not satisfy the
    contract)."""
    rels = (Relationship("p", ("id",), "c", ("pid",), namespace="ns"),)
    with pytest.raises(PlanCompileError) as ei:
        check_orphan_fk_policy_completeness(config, rels)
    assert ei.value.code == "orphan_fk_policy_missing"


def test_same_parent_different_child_may_differ() -> None:
    """The 4-tuple key (S13 P1): one parent, two children, different policies
    is LEGITIMATE, not a duplicate."""
    rels = (
        Relationship("emp", ("id",), "rev", ("employee_id",), namespace="ns"),
        Relationship("emp", ("id",), "rev", ("reviewer_id",), namespace="ns"),
    )
    config = _config_for(
        rels,
        {
            ("emp", ("id",), "rev", ("employee_id",)): "preserve",
            ("emp", ("id",), "rev", ("reviewer_id",)): "remap",
        },
    )
    resolved = check_orphan_fk_policy_completeness(config, rels)
    assert resolved[("emp", ("id",), "rev", ("employee_id",))] is OrphanPolicy.PRESERVE
    assert resolved[("emp", ("id",), "rev", ("reviewer_id",))] is OrphanPolicy.REMAP
