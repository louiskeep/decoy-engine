"""Plan-compile checks: the foundational validation set.

Each check is a pure function taking `(config, profile)` (and sometimes
additional precomputed state) and either returning silently on pass or
raising `PlanCompileError` on fail. The full check map lives in the
compile-check ownership table (S1 spec §plan-yaml-shape).

S2 relocated two relationship-related checks into
`decoy_engine.relationships`: `namespace_ambiguity` (now performed by
`build_namespace_registry`) and `fk_plan_ordering` (now performed by
`build_relationship_graph`). The check names still appear in
`PlanCompileResult.checks_passed` to preserve the S1 -> S2 regression
contract (per S2 spec B1: `checks_passed` equals S1's list plus exactly
one new entry, `orphan_fk_policy_completeness`).

`orphan_fk_policy_completeness` (new in S2, row 6) lives in
`decoy_engine.relationships._graph.check_orphan_fk_policy_completeness`
alongside the graph builder that consumes its lookup output.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._registry_stub import S1_STUB_REGISTRY
from decoy_engine.profile._types import Profile


def check_unknown_provider(config: dict[str, Any]) -> None:
    """Reject configs that reference a provider not in S1_STUB_REGISTRY.

    Compile-check ownership table row #2; S4 swaps the stub for the real
    registry behind the same check signature.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            provider = col_entry.get("provider")
            if provider is None:
                continue
            if provider not in S1_STUB_REGISTRY:
                col_name = col_entry.get("name", "?")
                raise PlanCompileError(
                    code="unknown_provider",
                    path=f"tables.{table_name}.columns.{col_name}.provider",
                    message=(
                        f"Provider {provider!r} is not in S1_STUB_REGISTRY. "
                        f"Add it to decoy_engine.plan._registry_stub.S1_STUB_REGISTRY or "
                        "use one of the registered names. Custom providers land in S4."
                    ),
                )


def check_basic_uniqueness_pre_flight(config: dict[str, Any], profile: Profile) -> None:
    """Reject pool-backed `unique` configs whose source distinct count
    exceeds the pool capacity hint.

    Partial in S1; S5 tightens with the full `pool_capacity_pre_flight`
    check. S1's check uses whatever capacity hint is available at compile
    time; if no hint is declared, the check passes (the runtime
    discovers the failure later).

    Compile-check ownership table row #4.
    """
    distinct_lookup: dict[tuple[str, str], int | None] = {}
    for table in profile.tables:
        for col in table.columns:
            distinct_lookup[(table.name, col.name)] = col.distinct_count

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("cardinality_mode") != "unique":
                continue
            if col_entry.get("backend_type") != "pool":
                continue
            pool_size = col_entry.get("pool_size")
            if pool_size is None:
                continue
            col_name = col_entry.get("name", "?")
            source_distinct = distinct_lookup.get((table_name, col_name))
            if source_distinct is None:
                continue
            if source_distinct > pool_size:
                raise PlanCompileError(
                    code="pool_capacity_pre_flight_unique",
                    path=f"tables.{table_name}.columns.{col_name}",
                    message=(
                        f"Column {table_name}.{col_name} uses cardinality_mode=unique "
                        f"with pool_size={pool_size}, but the profile reports "
                        f"distinct_count={source_distinct} source rows. The pool "
                        "cannot supply enough unique values; raise pool_size or pick "
                        "a different cardinality_mode."
                    ),
                )


def check_composite_columns_length_match(profile: Profile) -> None:
    """Every relationship's parent.columns and each child.columns must
    have the same length.

    The Profile-layer `Relationship` dataclass enforces this at construction
    time; this check exists at the planner layer too so a Profile that was
    hand-constructed via dict (e.g. through deserialization without going
    through `Relationship.__post_init__`) gets caught here.

    Compile-check ownership table row #5.
    """
    for rel in profile.relationships:
        parent_len = len(rel.parent_columns)
        child_len = len(rel.child_columns)
        if parent_len != child_len:
            raise PlanCompileError(
                code="composite_columns_length_mismatch",
                path=(
                    f"relationships[{rel.parent_table}.{rel.parent_columns}->"
                    f"{rel.child_table}.{rel.child_columns}]"
                ),
                message=(
                    f"Relationship {rel.parent_table}.{rel.parent_columns} -> "
                    f"{rel.child_table}.{rel.child_columns}: parent columns length "
                    f"{parent_len} != child columns length {child_len}."
                ),
            )
