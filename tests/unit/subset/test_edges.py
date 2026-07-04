from __future__ import annotations

from decoy_engine.plan._types import PlanRelationship, PlanRelationshipEnd
from decoy_engine.subset._edges import build_subset_edges


def test_expands_multi_child_relationship_into_sorted_edges() -> None:
    relationship = PlanRelationship(
        parent=PlanRelationshipEnd(table="customers", columns=("id",)),
        children=(
            PlanRelationshipEnd(table="orders", columns=("customer_id",)),
            PlanRelationshipEnd(table="invoices", columns=("customer_id",)),
        ),
        orphan_policy="preserve",
        namespace=None,
    )
    edges = build_subset_edges((relationship,))
    assert len(edges) == 2
    assert [e.child_table for e in edges] == ["invoices", "orders"]  # sorted by edge_id
    assert edges[0].edge_id == "customers.id -> invoices.customer_id"
    assert edges[1].edge_id == "customers.id -> orders.customer_id"


def test_dedupes_duplicate_relationship_declarations() -> None:
    relationship = PlanRelationship(
        parent=PlanRelationshipEnd(table="customers", columns=("id",)),
        children=(PlanRelationshipEnd(table="orders", columns=("customer_id",)),),
        orphan_policy="preserve",
        namespace=None,
    )
    edges = build_subset_edges((relationship, relationship))
    assert len(edges) == 1


def test_edge_id_format_matches_run_fk_validity_relationship_string() -> None:
    relationship = PlanRelationship(
        parent=PlanRelationshipEnd(table="a", columns=("k1", "k2")),
        children=(PlanRelationshipEnd(table="b", columns=("k1", "k2")),),
        orphan_policy="preserve",
        namespace=None,
    )
    edges = build_subset_edges((relationship,))
    assert edges[0].edge_id == "a.k1,k2 -> b.k1,k2"
