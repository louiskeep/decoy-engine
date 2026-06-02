"""Golden fixture relational invariants for the five S2-gated fixtures.

Per S2 spec §5: relationship-specific assertions (ordering respected,
namespace separated, orphan policy enforced, composite tuple resolved
as one node) for `relational_parent_child`, `composite_key`,
`nullable_fk`, `orphan_fk`, `repeated_across_tables`. Plus the
multi-parent FK synthetic config (H2 resolution) and the synthetic
namespace-separation case for `repeated_across_tables` (which has no
declared relationships, so the test exercises the namespace registry
directly).

These tests exercise `compile_plan` against profiles + configs that
mirror each fixture's relationship shape. The CSV files themselves are
exercised by `test_fixture_loads.py` (S1 slice 4); this file exercises
the planner's response to fixture-shaped relationships.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml
from tests.fixtures.golden._manifest_schema import FixtureManifest

from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.profile import (
    ColumnProfile,
    Profile,
    Relationship,
    TableProfile,
)

GOLDEN_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures" / "golden"


def _col(
    name: str,
    *,
    row_count: int = 10,
    null_count: int = 0,
    distinct_count: int | None = 10,
    is_fk: bool = False,
    fk_target: tuple[str, str] | None = None,
    declared_pk: bool = False,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype="object",
        row_count=row_count,
        null_count=null_count,
        distinct_count=distinct_count,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=declared_pk,
        is_fk=is_fk,
        fk_target=fk_target,
        pii_class=None,
    )


def _load_manifest(name: str) -> FixtureManifest:
    """Load the fixture's manifest.yaml and validate via Pydantic."""
    with (GOLDEN_ROOT / name / "manifest.yaml").open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return FixtureManifest(**data)


@pytest.mark.golden
class TestRelationalParentChildInvariants:
    """relational_parent_child: customers + orders + invoices + addresses.
    Graph ordering must put customers before each child table."""

    def test_compile_orders_customers_before_each_child(self) -> None:
        # Build a profile that mirrors the fixture shape.
        customers = TableProfile(
            name="customers",
            row_count=100,
            columns=(_col("customer_id", row_count=100, distinct_count=100, declared_pk=True),),
        )
        orders = TableProfile(
            name="orders",
            row_count=500,
            columns=(
                _col(
                    "customer_id",
                    row_count=500,
                    is_fk=True,
                    fk_target=("customers", "customer_id"),
                    distinct_count=100,
                ),
            ),
        )
        invoices = TableProfile(
            name="invoices",
            row_count=300,
            columns=(
                _col(
                    "customer_id",
                    row_count=300,
                    is_fk=True,
                    fk_target=("customers", "customer_id"),
                    distinct_count=100,
                ),
            ),
        )
        addresses = TableProfile(
            name="addresses",
            row_count=250,
            columns=(
                _col(
                    "customer_id",
                    row_count=250,
                    is_fk=True,
                    fk_target=("customers", "customer_id"),
                    distinct_count=100,
                ),
            ),
        )
        rels = tuple(
            Relationship(
                parent_table="customers",
                parent_columns=("customer_id",),
                child_table=child,
                child_columns=("customer_id",),
                namespace="customer_identity",
            )
            for child in ("orders", "invoices", "addresses")
        )
        profile = Profile(
            schema_version=1,
            tables=(customers, orders, invoices, addresses),
            relationships=rels,
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        manifest = _load_manifest("relational_parent_child")
        # Build config from the manifest's relationships (carries the
        # orphan_policy + namespace).
        config = _config_from_manifest(manifest)
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        nodes = [(o.table, o.columns) for o in plan.ordering]
        parent_pos = nodes.index(("customers", ("customer_id",)))
        for child in ("orders", "invoices", "addresses"):
            child_pos = nodes.index((child, ("customer_id",)))
            assert parent_pos < child_pos


@pytest.mark.golden
class TestCompositeKeyInvariants:
    """composite_key: composite tuple is a single ordering node."""

    def test_composite_tuple_is_single_node(self) -> None:
        enrollments = TableProfile(
            name="enrollments",
            row_count=200,
            columns=(
                _col("member_id", row_count=200, distinct_count=200, declared_pk=True),
                _col("plan_id", row_count=200, distinct_count=50, declared_pk=True),
                _col("effective_date", row_count=200, distinct_count=200, declared_pk=True),
            ),
        )
        claims = TableProfile(
            name="claims",
            row_count=1000,
            columns=(
                _col(
                    "member_id",
                    row_count=1000,
                    is_fk=True,
                    fk_target=("enrollments", "member_id"),
                    distinct_count=200,
                ),
                _col(
                    "plan_id",
                    row_count=1000,
                    is_fk=True,
                    fk_target=("enrollments", "plan_id"),
                    distinct_count=50,
                ),
                _col(
                    "effective_date",
                    row_count=1000,
                    is_fk=True,
                    fk_target=("enrollments", "effective_date"),
                    distinct_count=200,
                ),
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(enrollments, claims),
            relationships=(
                Relationship(
                    parent_table="enrollments",
                    parent_columns=("member_id", "plan_id", "effective_date"),
                    child_table="claims",
                    child_columns=("member_id", "plan_id", "effective_date"),
                    namespace="enrollment_identity",
                ),
            ),
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        config = _config_from_manifest(_load_manifest("composite_key"))
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        nodes = [(o.table, o.columns) for o in plan.ordering]
        assert ("enrollments", ("member_id", "plan_id", "effective_date")) in nodes
        assert ("enrollments", ("member_id",)) not in nodes


@pytest.mark.golden
class TestNullableFkInvariants:
    """nullable_fk: graph layer must not require non-null FKs."""

    def test_nullable_fk_compiles(self) -> None:
        employees = TableProfile(
            name="employees",
            row_count=50,
            columns=(_col("employee_id", row_count=50, distinct_count=50, declared_pk=True),),
        )
        reviews = TableProfile(
            name="reviews",
            row_count=200,
            columns=(
                _col(
                    "employee_id",
                    row_count=200,
                    is_fk=True,
                    fk_target=("employees", "employee_id"),
                    distinct_count=50,
                ),
                _col(
                    "reviewer_id",
                    row_count=200,
                    null_count=40,
                    is_fk=True,
                    fk_target=("employees", "employee_id"),
                    distinct_count=40,
                ),
            ),
        )
        rels = (
            Relationship(
                parent_table="employees",
                parent_columns=("employee_id",),
                child_table="reviews",
                child_columns=("employee_id",),
                namespace="employee_identity",
            ),
            Relationship(
                parent_table="employees",
                parent_columns=("employee_id",),
                child_table="reviews",
                child_columns=("reviewer_id",),
                namespace="employee_identity",
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(employees, reviews),
            relationships=rels,
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        config = _config_from_manifest(_load_manifest("nullable_fk"))
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        # Both relationships present in the plan.
        nodes = [(o.table, o.columns) for o in plan.ordering]
        assert ("reviews", ("employee_id",)) in nodes
        assert ("reviews", ("reviewer_id",)) in nodes


@pytest.mark.golden
class TestOrphanFkInvariants:
    """orphan_fk: each orphan_policy variant compiles; a missing policy fails."""

    @pytest.mark.parametrize("policy", ["preserve", "remap", "warn", "fail"])
    def test_each_orphan_policy_value_compiles(self, policy: str) -> None:
        customers = TableProfile(
            name="customers",
            row_count=50,
            columns=(_col("customer_id", row_count=50, distinct_count=50, declared_pk=True),),
        )
        orders = TableProfile(
            name="orders",
            row_count=100,
            columns=(
                _col(
                    "customer_id",
                    row_count=100,
                    is_fk=True,
                    fk_target=("customers", "customer_id"),
                    distinct_count=60,
                ),
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(customers, orders),
            relationships=(
                Relationship(
                    parent_table="customers",
                    parent_columns=("customer_id",),
                    child_table="orders",
                    child_columns=("customer_id",),
                    namespace="customer_identity",
                ),
            ),
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        config = {
            "global_settings": {"seed": 1},
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": policy,
                    "namespace": "customer_identity",
                }
            ],
        }
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        assert plan.relationships[0].orphan_policy == policy

    def test_missing_orphan_policy_fails_compile(self) -> None:
        customers = TableProfile(
            name="customers",
            row_count=50,
            columns=(_col("customer_id", row_count=50, distinct_count=50, declared_pk=True),),
        )
        orders = TableProfile(
            name="orders",
            row_count=100,
            columns=(
                _col(
                    "customer_id",
                    row_count=100,
                    is_fk=True,
                    fk_target=("customers", "customer_id"),
                    distinct_count=60,
                ),
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(customers, orders),
            relationships=(
                Relationship(
                    parent_table="customers",
                    parent_columns=("customer_id",),
                    child_table="orders",
                    child_columns=("customer_id",),
                    namespace="customer_identity",
                ),
            ),
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        config_missing_policy = {
            "global_settings": {"seed": 1},
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["customer_id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    # no orphan_policy
                    "namespace": "customer_identity",
                }
            ],
        }
        with pytest.raises(PlanCompileError) as excinfo:
            compile_plan(config_missing_policy, profile, decoy_engine_version="0.1.0")
        assert excinfo.value.code == "orphan_fk_policy_missing"


@pytest.mark.golden
class TestRepeatedAcrossTablesInvariants:
    """repeated_across_tables: primary_emails.email and login_emails.email.
    Same value, different namespaces -> different masked values.
    """

    def test_separate_namespaces_keep_emails_independent(self) -> None:
        primary = TableProfile(
            name="primary_emails",
            row_count=100,
            columns=(_col("email", row_count=100, distinct_count=80),),
        )
        login = TableProfile(
            name="login_emails",
            row_count=100,
            columns=(_col("email", row_count=100, distinct_count=80),),
        )
        profile = Profile(
            schema_version=1,
            tables=(primary, login),
            relationships=(),
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        config = {
            "global_settings": {"seed": 1},
            "namespaces": {
                "primary_pool": {"declared_by": ["primary_emails.email"]},
                "login_pool": {"declared_by": ["login_emails.email"]},
            },
        }
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        # Both namespaces appear in the plan; they're independent slots.
        ns_names = {ns.namespace for ns in plan.namespaces}
        assert "primary_pool" in ns_names
        assert "login_pool" in ns_names

    def test_same_namespace_declared_on_both_columns_unifies(self) -> None:
        """Counter-test: when both columns explicitly share a namespace,
        they end up in the same registry binding."""
        primary = TableProfile(
            name="primary_emails",
            row_count=100,
            columns=(_col("email", row_count=100, distinct_count=80),),
        )
        login = TableProfile(
            name="login_emails",
            row_count=100,
            columns=(_col("email", row_count=100, distinct_count=80),),
        )
        profile = Profile(
            schema_version=1,
            tables=(primary, login),
            relationships=(),
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        config = {
            "global_settings": {"seed": 1},
            "namespaces": {
                "shared_email_pool": {
                    "declared_by": ["primary_emails.email", "login_emails.email"]
                },
            },
        }
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        ns_names = {ns.namespace for ns in plan.namespaces}
        assert "shared_email_pool" in ns_names
        # Only one namespace; both columns share it.
        shared = next(n for n in plan.namespaces if n.namespace == "shared_email_pool")
        tables_bound = {t for (t, _) in shared.declared_by}
        assert tables_bound == {"primary_emails", "login_emails"}


@pytest.mark.golden
class TestMultiParentFkSyntheticConfig:
    """S2 spec H2 resolution: an inline config that declares child.col as a
    FK to two parents must fail compile with multi_parent_fk_unsupported."""

    def test_multi_parent_fk_rejected_through_compile_plan(self) -> None:
        parent_a = TableProfile(
            name="parent_a",
            row_count=10,
            columns=(_col("id", declared_pk=True),),
        )
        parent_b = TableProfile(
            name="parent_b",
            row_count=10,
            columns=(_col("id", declared_pk=True),),
        )
        child = TableProfile(
            name="child",
            row_count=20,
            columns=(
                _col(
                    "id",
                    row_count=20,
                    is_fk=True,
                    fk_target=("parent_a", "id"),
                    distinct_count=15,
                ),
            ),
        )
        # Use the same namespace on both relationships so the namespace
        # registry doesn't fire ambiguity first; the test is specifically
        # about the multi_parent_fk_unsupported rejection downstream.
        rels = (
            Relationship(
                parent_table="parent_a",
                parent_columns=("id",),
                child_table="child",
                child_columns=("id",),
                namespace="shared_ns",
            ),
            Relationship(
                parent_table="parent_b",
                parent_columns=("id",),
                child_table="child",
                child_columns=("id",),
                namespace="shared_ns",
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(parent_a, parent_b, child),
            relationships=rels,
            profiled_at=datetime(2026, 5, 27, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        config = {
            "global_settings": {"seed": 1},
            "relationships": [
                {
                    "parent": {"table": "parent_a", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["id"]}],
                    "orphan_policy": "fail",
                    "namespace": "shared_ns",
                },
                {
                    "parent": {"table": "parent_b", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["id"]}],
                    "orphan_policy": "fail",
                    "namespace": "shared_ns",
                },
            ],
        }
        with pytest.raises(PlanCompileError) as excinfo:
            compile_plan(config, profile, decoy_engine_version="0.1.0")
        assert excinfo.value.code == "multi_parent_fk_unsupported"


@pytest.mark.golden
class TestSelfFkInvariants:
    """self_fk (FC-2): single-table self-FK with distinct parent + child
    columns compiles to a topo ordering where the parent column is
    ordered BEFORE the child column. The graph keys nodes by
    (table, column_tuple), so a self-FK on the same table with different
    columns is two distinct nodes (industry standard pattern: SDV HMA1
    "parent-then-child" within one table)."""

    def test_self_fk_compile_orders_parent_before_child(self) -> None:
        employees = TableProfile(
            name="employees",
            row_count=50,
            columns=(
                _col("id", row_count=50, distinct_count=50, declared_pk=True),
                _col(
                    "manager_id",
                    row_count=50,
                    null_count=5,
                    is_fk=True,
                    fk_target=("employees", "id"),
                    distinct_count=10,
                ),
            ),
        )
        rels = (
            Relationship(
                parent_table="employees",
                parent_columns=("id",),
                child_table="employees",
                child_columns=("manager_id",),
                namespace="employee_identity",
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(employees,),
            relationships=rels,
            profiled_at=datetime(2026, 6, 2, 0, 0, 0),
            decoy_engine_version="0.1.0",
        )
        config = _config_from_manifest(_load_manifest("self_fk"))
        plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
        nodes = [(o.table, o.columns) for o in plan.ordering]
        parent_idx = nodes.index(("employees", ("id",)))
        child_idx = nodes.index(("employees", ("manager_id",)))
        assert parent_idx < child_idx, (
            f"self-FK topo broken: parent ('employees', ('id',)) at {parent_idx}, "
            f"child ('employees', ('manager_id',)) at {child_idx}; "
            f"ordering={nodes}"
        )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _config_from_manifest(manifest: FixtureManifest) -> dict:
    """Translate a fixture's manifest.yaml into a compile_plan config dict.

    Only emits the `relationships` + `global_settings` blocks needed for
    compile_plan to find orphan_policy + namespaces. Tables blocks are
    not built because the planner only needs them for masking strategies,
    which these invariant tests don't exercise.
    """
    return {
        "global_settings": {"seed": 1},
        "relationships": [
            {
                "parent": {
                    "table": rel.parent.table,
                    "columns": list(rel.parent.columns),
                },
                "children": [{"table": c.table, "columns": list(c.columns)} for c in rel.children],
                "orphan_policy": rel.orphan_policy,
                "namespace": rel.namespace,
            }
            for rel in manifest.relationships
        ],
    }
