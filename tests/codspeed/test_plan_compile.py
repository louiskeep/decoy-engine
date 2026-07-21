"""CodSpeed: FK-plan compile
(``decoy_engine.plan.compile_plan``) -- the config+profile -> frozen-Plan
step every masking/generation job runs once before execution starts.

Exercises the full compile-time check chain (namespace registry, FK
ordering + orphan-policy completeness, uniqueness/pool-capacity pre-flight,
composite-key wiring, and the rest of the ~30 checks_passed rows -- see
tests/unit/plan/test_compile_basic.py for the authoritative list) against a
two-table parent/child profile, the same shape
tests/unit/plan/conftest.py's `simple_profile`/`simple_config` fixtures use
for compile correctness tests. Built in-memory (no file I/O, no profiling
scan) so the benchmark isolates compile cost from source-read cost.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from decoy_engine.plan import compile_plan
from decoy_engine.profile import ColumnProfile, Profile, Relationship, TableProfile

pytestmark = pytest.mark.codspeed


def _col(
    name: str,
    *,
    row_count: int = 10,
    distinct_count: int | None = 10,
    declared_pk: bool = False,
    is_candidate_key_sampled: bool = False,
    is_fk: bool = False,
    fk_target: tuple[str, str] | None = None,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype="object",
        row_count=row_count,
        null_count=0,
        distinct_count=distinct_count,
        sampled=False,
        is_candidate_key_sampled=is_candidate_key_sampled,
        declared_pk=declared_pk,
        is_fk=is_fk,
        fk_target=fk_target,
        pii_class=None,
    )


def _profile() -> Profile:
    customers = TableProfile(
        name="customers",
        row_count=10,
        columns=(
            _col("customer_id", declared_pk=True, is_candidate_key_sampled=True),
            _col("name"),
            _col("email", distinct_count=10),
        ),
    )
    orders = TableProfile(
        name="orders",
        row_count=20,
        columns=(
            _col(
                "order_id",
                row_count=20,
                declared_pk=True,
                is_candidate_key_sampled=True,
                distinct_count=20,
            ),
            _col(
                "customer_id",
                row_count=20,
                is_fk=True,
                fk_target=("customers", "customer_id"),
                distinct_count=10,
            ),
        ),
    )
    return Profile(
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


def _config() -> dict:
    return {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {
                        "name": "name",
                        "strategy": "faker_name",
                        "provider": "person_name",
                        "cardinality_mode": "reuse",
                    },
                    {"name": "email", "strategy": "hash", "namespace": "email_ns"},
                ],
            }
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["customer_id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "fail",
                "namespace": "customer_identity",
            }
        ],
        "namespaces": {
            "customer_identity": {"declared_by": ["customers.customer_id", "orders.customer_id"]}
        },
    }


_PROFILE = _profile()
_CONFIG = _config()


def test_compile_plan_two_table_fk(benchmark) -> None:
    def _run():
        return compile_plan(_CONFIG, _PROFILE, decoy_engine_version="codspeed-bench")

    plan = benchmark(_run)

    assert plan.engine_version == "codspeed-bench"
    assert "fk_plan_ordering" in plan.plan_compile.checks_passed
    assert "orphan_fk_policy_completeness" in plan.plan_compile.checks_passed
