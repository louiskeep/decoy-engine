from __future__ import annotations

import pytest

from decoy_engine.subset._errors import SubsetConfigError
from decoy_engine.subset._policy import resolve_edge_directions
from decoy_engine.subset._types import FanOutPolicy, SubsetEdge

EDGE = SubsetEdge(
    edge_id="customers.id -> orders.customer_id",
    parent_table="customers",
    parent_columns=("id",),
    child_table="orders",
    child_columns=("customer_id",),
    orphan_policy="preserve",
    namespace=None,
)


def test_default_direction_is_both() -> None:
    directions = resolve_edge_directions((EDGE,), FanOutPolicy())
    assert directions[EDGE.edge_id] == "both"


def test_downward_without_allow_dangling_raises() -> None:
    policy = FanOutPolicy(edge_directions=((EDGE.edge_id, "downward"),))
    with pytest.raises(SubsetConfigError) as excinfo:
        resolve_edge_directions((EDGE,), policy)
    assert excinfo.value.code == "subset_dangling_not_acknowledged"


def test_downward_with_allow_dangling_passes() -> None:
    policy = FanOutPolicy(edge_directions=((EDGE.edge_id, "downward"),), allow_dangling=True)
    directions = resolve_edge_directions((EDGE,), policy)
    assert directions[EDGE.edge_id] == "downward"


def test_upward_only_never_dangles_no_acknowledgement_needed() -> None:
    policy = FanOutPolicy(edge_directions=((EDGE.edge_id, "upward"),))
    directions = resolve_edge_directions((EDGE,), policy)
    assert directions[EDGE.edge_id] == "upward"


def test_unknown_edge_id_raises() -> None:
    policy = FanOutPolicy(edge_directions=(("bogus.edge -> id", "both"),))
    with pytest.raises(SubsetConfigError) as excinfo:
        resolve_edge_directions((EDGE,), policy)
    assert excinfo.value.code == "subset_unknown_edge"
