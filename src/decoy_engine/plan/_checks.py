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

import re
from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Profile

# Strategies under which a null-bearing integer source column diverges across the
# pandas oracle and the polars-native path (to_pandas widens int+null to float64,
# which the deterministic remap then either reshapes or hard-errors on, while the
# polars path keeps the integer). B1, PO-settled 2026-05-28: reject at validation.
_INT_NULL_REJECTED_STRATEGIES = frozenset({"truncate", "hash", "categorical"})


def _is_integer_dtype(dtype: str) -> bool:
    """True for pandas/numpy/arrow/DB integer dtype strings.

    `ColumnProfile.dtype` is `str(series.dtype)`, so it can be `int64`,
    `int64[pyarrow]`, `Int64` (nullable), `uint32`, or a DB-source label like
    `integer` / `bigint` / `smallint`. Floats, object, datetime, interval, and
    boolean are excluded.
    """
    base = dtype.lower().split("[", 1)[0].strip()
    if base in {"integer", "bigint", "smallint", "tinyint", "int"}:
        return True
    return bool(re.fullmatch(r"u?int(8|16|32|64)?", base))


def check_unknown_provider(config: dict[str, Any]) -> None:
    """Reject configs that reference a provider not in the registry.

    Compile-check ownership table row #2. S1 shipped this against
    `S1_STUB_REGISTRY`; S4 swapped to `get_default_registry().known_providers()`
    (the real registry from `decoy_engine.providers_v2`). Behavior contract is
    preserved: same configs accepted, same configs rejected against the
    registered set; the test signature shape changed (per S4 spec §9 + cold-
    read M4).

    The registry import is deferred inside the function. The real motivation
    is import-cycle prevention: `decoy_engine.providers_v2` and the planner
    sit on the same dependency tier, and a module-level import here can
    surface a cycle as the package grows. Faker eagerness is not the issue
    (faker is already loaded by other engine modules at package import time);
    cycle prevention is. Dennis Session 22 L1.
    """
    from decoy_engine.providers_v2 import get_default_registry

    known = get_default_registry().known_providers()
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
            if provider not in known:
                col_name = col_entry.get("name", "?")
                raise PlanCompileError(
                    code="unknown_provider",
                    path=f"tables.{table_name}.columns.{col_name}.provider",
                    message=(
                        f"Provider {provider!r} is not in the default registry. "
                        f"Known providers: {sorted(known)!r}. Custom providers "
                        "land via `register_faker_provider_v2` (V2) or "
                        "`register_faker_provider` (V1; until S9)."
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


def check_null_bearing_int_unsupported(config: dict[str, Any], profile: Profile) -> None:
    """Reject integer + null-bearing source columns under truncate/hash/categorical.

    Compile-check ownership table row #10 (B1, S13). PO-settled 2026-05-28: a
    column that is integer-typed AND null-bearing is REJECTED at plan-compile when
    masked under truncate / hash / categorical, because its masked value is
    ambiguous across execution substrates (`to_pandas()` widens int+null to
    float64; the polars-native path keeps the integer). This is the same class of
    "ambiguous numeric source" the S5 float-canonicalization hard error already
    rejects. Remediation: stringify or bin the column upstream.

    Profile-dependent (reads `dtype` + `null_count`), so under `no_profile=True`
    it lands in `checks_skipped`; the execution-time guard
    (`decoy_engine.execution` `reject_null_bearing_int`) is the backstop there.
    """
    null_int_lookup: dict[tuple[str, str], bool] = {}
    for table in profile.tables:
        for col in table.columns:
            null_int_lookup[(table.name, col.name)] = (
                _is_integer_dtype(col.dtype) and col.null_count > 0
            )

    # FK-child columns are EXEMPT: they are resolved through the relationship edge
    # (not masked by the strategy), and an FK job runs via the pandas oracle on
    # both substrates, so the int+null divergence cannot arise for them. Matches
    # the execution-time guard's FK exemption.
    fk_child_columns: set[tuple[str, str]] = {
        (rel.child_table, child_col)
        for rel in profile.relationships
        for child_col in rel.child_columns
    }

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") not in _INT_NULL_REJECTED_STRATEGIES:
                continue
            col_name = col_entry.get("name", "?")
            if (table_name, col_name) in fk_child_columns:
                continue
            if not null_int_lookup.get((table_name, col_name), False):
                continue
            raise PlanCompileError(
                code="null_bearing_int_unsupported",
                path=f"tables.{table_name}.columns.{col_name}",
                message=(
                    f"Column {table_name}.{col_name} is an integer column with nulls "
                    f"masked under {col_entry.get('strategy')!r}. Integer-with-null is "
                    "not supported under truncate/hash/categorical: the masked value is "
                    "ambiguous across execution substrates (int widens to float on one "
                    "path, stays integer on the other). Stringify or bin this column "
                    "upstream. This mirrors the float-canonicalization hard error."
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
