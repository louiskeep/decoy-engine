"""Unit tests for decoy_engine.relationships._namespace.

Covers the S2 spec §Tests "Namespace registry" block plus the H1
resolution (FK-no-resolution clause of namespace_missing).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import (
    Profile,
    Relationship,
)
from decoy_engine.relationships import (
    NamespaceConfigError,
    NamespaceRegistry,
    build_namespace_registry,
)


def _bare_profile(relationships: tuple[Relationship, ...] = ()) -> Profile:
    """Smallest valid Profile shape; no columns/tables needed for namespace tests."""
    return Profile(
        schema_version=1,
        tables=(),
        relationships=relationships,
        profiled_at=datetime(2026, 5, 27, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


class TestFkAutoBinding:
    def test_fk_relationship_auto_binds_child_into_parent_namespace(
        self, parent_child_profile: Profile
    ) -> None:
        registry = build_namespace_registry({}, parent_child_profile)
        assert registry.for_column("customers", ("customer_id",)) == "customer_identity"
        assert registry.for_column("orders", ("customer_id",)) == "customer_identity"

    def test_composite_columns_appear_as_one_binding_entry(
        self, composite_profile: Profile
    ) -> None:
        registry = build_namespace_registry({}, composite_profile)
        members = registry.members("enrollment_identity")
        # Both parent + child should be one entry each (the composite tuple).
        assert ("enrollments", ("member_id", "plan_id", "effective_date")) in members
        assert ("claims", ("member_id", "plan_id", "effective_date")) in members
        # Composite is NOT broken into single-column entries.
        assert ("enrollments", ("member_id",)) not in members
        assert ("enrollments", ("plan_id",)) not in members


class TestExplicitDeclarations:
    def test_explicit_namespace_declarations_loaded(self) -> None:
        config = {
            "namespaces": {
                "logins": {"declared_by": ["sessions.user_email"]},
            }
        }
        registry = build_namespace_registry(config, _bare_profile())
        assert registry.for_column("sessions", ("user_email",)) == "logins"

    def test_explicit_and_fk_inherited_coexist(self, parent_child_profile: Profile) -> None:
        config = {
            "namespaces": {
                "logins": {"declared_by": ["sessions.user_email"]},
            }
        }
        registry = build_namespace_registry(config, parent_child_profile)
        assert registry.for_column("sessions", ("user_email",)) == "logins"
        assert registry.for_column("orders", ("customer_id",)) == "customer_identity"
        # Two distinct namespaces in the registry.
        assert "logins" in registry.declared()
        assert "customer_identity" in registry.declared()


class TestAmbiguity:
    def test_column_declared_in_two_namespaces_raises(self) -> None:
        config = {
            "namespaces": {
                "a": {"declared_by": ["t.col"]},
                "b": {"declared_by": ["t.col"]},
            }
        }
        with pytest.raises(NamespaceConfigError) as excinfo:
            build_namespace_registry(config, _bare_profile())
        assert excinfo.value.code == "namespace_ambiguity"

    def test_ambiguity_is_subclass_of_plan_compile_error(self) -> None:
        """S2 TODO 5: NamespaceConfigError is subclass of PlanCompileError so
        callers writing `except PlanCompileError` catch both."""
        config = {
            "namespaces": {
                "a": {"declared_by": ["t.col"]},
                "b": {"declared_by": ["t.col"]},
            }
        }
        with pytest.raises(PlanCompileError):
            build_namespace_registry(config, _bare_profile())

    def test_fk_override_to_different_namespace_raises(self) -> None:
        """S2 TODO 2: explicit override of FK-inherited namespace is rejected
        as namespace_ambiguity, not warned-and-applied."""
        # Profile declares the FK has namespace 'customer_identity'.
        rel = Relationship(
            parent_table="customers",
            parent_columns=("customer_id",),
            child_table="orders",
            child_columns=("customer_id",),
            namespace="customer_identity",
        )
        profile = _bare_profile(relationships=(rel,))
        # Config declares orders.customer_id in 'login_identity' (override).
        config = {
            "namespaces": {
                "login_identity": {"declared_by": ["orders.customer_id"]},
            }
        }
        with pytest.raises(NamespaceConfigError) as excinfo:
            build_namespace_registry(config, profile)
        assert excinfo.value.code == "namespace_ambiguity"


class TestDeterministicModeRequiresNamespace:
    def test_deterministic_column_without_namespace_raises(self) -> None:
        config = {
            "tables": [
                {
                    "name": "users",
                    "columns": [
                        {
                            "name": "email",
                            "deterministic": True,
                            "cardinality_mode": "reuse",
                            "strategy": "hash_email",
                            "provider": "person_email",
                        }
                    ],
                }
            ]
        }
        with pytest.raises(NamespaceConfigError) as excinfo:
            build_namespace_registry(config, _bare_profile())
        assert excinfo.value.code == "namespace_missing"

    def test_deterministic_column_with_explicit_namespace_passes(self) -> None:
        config = {
            "namespaces": {
                "logins": {"declared_by": ["users.email"]},
            },
            "tables": [
                {
                    "name": "users",
                    "columns": [
                        {
                            "name": "email",
                            "deterministic": True,
                            "cardinality_mode": "reuse",
                            "strategy": "hash_email",
                            "provider": "person_email",
                            "namespace": "logins",
                        }
                    ],
                }
            ],
        }
        registry = build_namespace_registry(config, _bare_profile())
        assert registry.for_column("users", ("email",)) == "logins"


class TestNamespaceMissingOnFkResolution:
    """S2 spec H1 resolution: namespace_missing fires when a Relationship
    has namespace=None and nothing in config supplies one either."""

    def test_unresolvable_fk_raises_via_for_relationship(self) -> None:
        rel = Relationship(
            parent_table="customers",
            parent_columns=("customer_id",),
            child_table="orders",
            child_columns=("customer_id",),
            namespace=None,
        )
        registry = build_namespace_registry({}, _bare_profile(relationships=(rel,)))
        # The registry built without raising (no orphan_policy or
        # ambiguity to flag); resolution-time call raises.
        with pytest.raises(NamespaceConfigError) as excinfo:
            registry.for_relationship(rel)
        assert excinfo.value.code == "namespace_missing"

    def test_explicit_parent_column_binding_resolves_for_namespaceless_relationship(self) -> None:
        rel = Relationship(
            parent_table="customers",
            parent_columns=("customer_id",),
            child_table="orders",
            child_columns=("customer_id",),
            namespace=None,
        )
        config = {
            "namespaces": {
                "customer_identity": {"declared_by": ["customers.customer_id"]},
            }
        }
        registry = build_namespace_registry(config, _bare_profile(relationships=(rel,)))
        # Even though the relationship has namespace=None, the parent
        # column's explicit binding satisfies for_relationship.
        assert registry.for_relationship(rel) == "customer_identity"


class TestQueryMethods:
    def test_for_column_returns_none_for_unbound_column(self) -> None:
        registry = build_namespace_registry({}, _bare_profile())
        assert registry.for_column("nowhere", ("nada",)) is None

    def test_members_enumerates_every_column_bound(self) -> None:
        config = {
            "namespaces": {
                "ns_a": {"declared_by": ["t1.col1", "t2.col2"]},
            }
        }
        registry = build_namespace_registry(config, _bare_profile())
        members = registry.members("ns_a")
        assert ("t1", ("col1",)) in members
        assert ("t2", ("col2",)) in members

    def test_members_returns_empty_for_unknown_namespace(self) -> None:
        registry = build_namespace_registry({}, _bare_profile())
        assert registry.members("nope") == ()

    def test_declared_returns_namespace_set(self) -> None:
        config = {
            "namespaces": {
                "ns_a": {"declared_by": ["t1.col1"]},
                "ns_b": {"declared_by": ["t2.col2"]},
            }
        }
        registry = build_namespace_registry(config, _bare_profile())
        assert registry.declared() == frozenset({"ns_a", "ns_b"})


class TestDeterminism:
    def test_two_builds_produce_equal_registries(self, parent_child_profile: Profile) -> None:
        r1 = build_namespace_registry({}, parent_child_profile)
        r2 = build_namespace_registry({}, parent_child_profile)
        assert r1 == r2

    def test_registry_is_frozen(self, parent_child_profile: Profile) -> None:
        from dataclasses import FrozenInstanceError

        registry = build_namespace_registry({}, parent_child_profile)
        with pytest.raises(FrozenInstanceError):
            registry.bindings = ()  # type: ignore[misc]


class TestNamespaceRegistryShape:
    def test_namespace_registry_is_a_namespace_registry(
        self, parent_child_profile: Profile
    ) -> None:
        registry = build_namespace_registry({}, parent_child_profile)
        assert isinstance(registry, NamespaceRegistry)
