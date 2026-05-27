"""Namespace declared_by serialization round-trip tests.

M1 fix regression tests (session 11 review finding): the pre-fix code used
'__' as a column separator in the declared_by string, causing column names
that themselves contain '__' to be split into spurious multi-column tuples.
The fix serializes declared_by as [[table, [col1, col2, ...]], ...] -- an
unambiguous structured list format.

These tests pin the correct behavior and would have caught the original bug.
All are pure serialization tests; no plan compile or profile scanning needed.
"""

from __future__ import annotations

import yaml

from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
from decoy_engine.plan._types import (
    NamespaceBinding,
    OrderingNode,
    Plan,
    PlanCompileResult,
    PlanRelationship,
    PlanRelationshipEnd,
    SeedEnvelope,
)


def _minimal_plan(namespaces: tuple[NamespaceBinding, ...]) -> Plan:
    """Build the smallest valid Plan that exercises namespace serialization."""
    return Plan(
        plan_version=1,
        seed_protocol_version=0,
        engine_version="0.1.0",
        pipeline_config_hash="a" * 64,
        profile_hash="b" * 64,
        seed_envelope=SeedEnvelope(job_seed=0),
        relationships=(
            PlanRelationship(
                parent=PlanRelationshipEnd(table="t", columns=("id",)),
                children=(PlanRelationshipEnd(table="c", columns=("t_id",)),),
                orphan_policy="preserve",
                namespace=None,
            ),
        ),
        namespaces=namespaces,
        ordering=(
            OrderingNode(table="t", columns=("id",)),
            OrderingNode(table="c", columns=("t_id",)),
        ),
        plan_compile=PlanCompileResult(),
    )


class TestNamespaceSerializationFormat:
    """Verify the serialized YAML shape uses [[table, [col...]], ...] format."""

    def test_single_column_serialized_as_list_not_string(self) -> None:
        """Single-column declared_by emits [[table, [col]]], not 'table.col' strings."""
        ns = NamespaceBinding(
            namespace="id_ns",
            declared_by=(("customers", ("customer_id",)),),
            seed=1,
        )
        plan = _minimal_plan((ns,))
        y = plan_to_yaml(plan)
        parsed = yaml.safe_load(y)
        declared_by = parsed["namespaces"]["id_ns"]["declared_by"]
        assert isinstance(declared_by, list), "declared_by must be a list"
        assert isinstance(declared_by[0], list), (
            f"Each entry must be a [table, cols] list, not {type(declared_by[0]).__name__!r}; "
            "got {declared_by[0]!r}"
        )
        assert declared_by[0][0] == "customers"
        assert declared_by[0][1] == ["customer_id"]

    def test_composite_key_serialized_as_list(self) -> None:
        """Composite-key declared_by emits [[table, [col1, col2, col3]]]."""
        ns = NamespaceBinding(
            namespace="enroll_ns",
            declared_by=(
                ("enrollments", ("member_id", "plan_id", "effective_date")),
            ),
            seed=2,
        )
        plan = _minimal_plan((ns,))
        y = plan_to_yaml(plan)
        parsed = yaml.safe_load(y)
        declared_by = parsed["namespaces"]["enroll_ns"]["declared_by"]
        assert declared_by[0][1] == ["member_id", "plan_id", "effective_date"]


class TestNamespaceRoundTrip:
    """Verify plan_from_yaml(plan_to_yaml(plan)) == plan for namespace cases."""

    def test_single_column_roundtrip(self) -> None:
        """Single-column namespace binding survives a serialize/deserialize cycle."""
        ns = NamespaceBinding(
            namespace="id_ns",
            declared_by=(("customers", ("customer_id",)),),
            seed=1,
        )
        plan = _minimal_plan((ns,))
        assert plan_from_yaml(plan_to_yaml(plan)) == plan

    def test_composite_key_roundtrip(self) -> None:
        """Composite-key namespace binding survives a serialize/deserialize cycle."""
        ns = NamespaceBinding(
            namespace="enroll_ns",
            declared_by=(
                ("enrollments", ("member_id", "plan_id", "effective_date")),
            ),
            seed=2,
        )
        plan = _minimal_plan((ns,))
        assert plan_from_yaml(plan_to_yaml(plan)) == plan

    def test_column_name_with_double_underscore_roundtrip(self) -> None:
        """M1 regression: column name containing '__' must NOT be split on deserialize.

        Under the old code, 'customers.account__id' deserialized as the tuple
        ('account', 'id'), producing a two-column composite instead of the
        intended single-column binding. The new [table, [col...]] list format
        is unambiguous regardless of what characters appear in column names.
        """
        ns = NamespaceBinding(
            namespace="account_ns",
            declared_by=(("customers", ("account__id",)),),  # single col with __ in name
            seed=3,
        )
        plan = _minimal_plan((ns,))
        recovered = plan_from_yaml(plan_to_yaml(plan))
        assert recovered == plan
        # Explicit check: the column tuple must have length 1, not 2.
        binding = recovered.namespaces[0]
        actual_cols = binding.declared_by[0][1]
        assert actual_cols == ("account__id",), (
            f"Expected single-element tuple ('account__id',), got {actual_cols!r}. "
            "This is the M1 regression: __ in column name was treated as a separator."
        )

    def test_multiple_declared_by_entries_roundtrip(self) -> None:
        """Multiple (table, cols) pairs in declared_by round-trip correctly."""
        ns = NamespaceBinding(
            namespace="customer_identity",
            declared_by=(
                ("customers", ("customer_id",)),
                ("orders", ("customer_id",)),
                ("invoices", ("customer_id",)),
            ),
            seed=4,
        )
        plan = _minimal_plan((ns,))
        assert plan_from_yaml(plan_to_yaml(plan)) == plan

    def test_multiple_namespaces_roundtrip(self) -> None:
        """Multiple namespace bindings in a plan round-trip correctly."""
        ns1 = NamespaceBinding(
            namespace="customer_identity",
            declared_by=(("customers", ("customer_id",)),),
            seed=1,
        )
        ns2 = NamespaceBinding(
            namespace="enrollment_identity",
            declared_by=(
                ("enrollments", ("member_id", "plan_id")),
                ("claims", ("member_id", "plan_id")),
            ),
            seed=2,
        )
        plan = _minimal_plan((ns1, ns2))
        assert plan_from_yaml(plan_to_yaml(plan)) == plan
