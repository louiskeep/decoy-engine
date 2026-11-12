"""`scope_for` is the one place that decides which supplied tables the subset narrows."""

from __future__ import annotations

from decoy_engine.subset import SubsetRequest, scope_for
from tests.unit.subset._fixtures_inputs import request
from tests.unit.subset.conftest import rel


def test_unlinked_table_is_whole_and_linked_tables_are_in_scope() -> None:
    scope = scope_for(request(), ["parent", "child", "lonely"])
    assert scope.tables == frozenset({"parent", "child"})
    assert scope.whole == frozenset({"lonely"})


def test_scope_follows_edges_in_both_directions() -> None:
    base = request()
    chained = SubsetRequest(
        relationships=(
            *base.relationships,
            rel("child", ("cid",), "grand", ("cid",)),
            rel("other", ("k",), "grand", ("k",)),
        ),
        seeds=base.seeds,
        policy=base.policy,
    )
    scope = scope_for(chained, ["parent", "child", "grand", "other", "lonely"])
    assert scope.tables == frozenset({"parent", "child", "grand", "other"})
    assert scope.whole == frozenset({"lonely"})


def test_tables_not_supplied_are_in_neither_set() -> None:
    scope = scope_for(request(), ["parent", "lonely"])
    assert scope.tables == frozenset({"parent"})
    assert scope.whole == frozenset({"lonely"})
