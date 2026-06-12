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
    if base in {"integer", "bigint", "smallint", "tinyint", "int", "intp", "uintp"}:
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


def check_non_poolable_provider_with_pool_backend(config: dict[str, Any]) -> None:
    """Reject pool-routed columns whose provider declares poolable=False.

    Compile-check ownership table row #11 (audit H5, 2026-06-12). Pool
    routing is structural: `strategy: faker` ALWAYS builds a pool
    (FakerStrategyHandler -> PoolBuilder), and PoolBuilder.build raises
    PoolCapacityError[provider_not_poolable] at runtime for any provider
    with `poolable: False` -- so a faker column on uuid/lorem-style
    providers is guaranteed dead at `run` while passing schema
    validation. This check moves that failure to compile time. The
    capacity pre-flight (row 7) deliberately SKIPS non-poolable
    providers, so nothing else catches the combination.

    Config + registry only (no profile): safe to run in --no-profile
    mode and in config-only validation paths.
    """
    from decoy_engine.providers_v2 import get_default_registry

    registry = get_default_registry()
    known = registry.known_providers()
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "faker":
                continue
            provider = col_entry.get("provider")
            if provider is None or provider not in known:
                continue  # unknown_provider (row 2) owns missing/unknown
            if not registry.get_capabilities(provider).poolable:
                col_name = col_entry.get("name", "?")
                raise PlanCompileError(
                    code="non_poolable_provider_with_pool_backend",
                    path=f"tables.{table_name}.columns.{col_name}.provider",
                    message=(
                        f"Provider {provider!r} declares poolable=False but column "
                        f"{table_name}.{col_name} uses strategy 'faker', which always "
                        "routes through the pool backend and fails at runtime with "
                        "provider_not_poolable. Use a poolable provider, a keyed "
                        "strategy (hash / fpe) for deterministic identifiers, or "
                        "redact."
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


def check_statistical_columns(config: dict[str, Any]) -> None:
    """Validate `type: statistical` generate columns against their snapshots.

    Compile-check ownership table row #12 (capability-gaps WS3,
    2026-06-12). A statistical column is guaranteed dead at run when its
    snapshot_file is unreadable, the source column is absent from the
    artifact, the snapshot kind has no sampler (an all-null "empty"
    column; freetext is admitted since deferred follow-up 4), a
    categorical column lacks the `allow_real_categories: true`
    disclosure opt-in, or
    `condition_on` names a pair the snapshot has no joint table for.
    `generation/statistical.load_spec` owns those verdicts (one set of
    error codes for compile time and generation time); this check adds
    the declared-order rule load_spec cannot see: the condition_on
    column must be generated BEFORE its dependent in the same table.

    Config + snapshot artifact only (no profile, no source data): the
    snapshot is a config-referenced fitted-model file, so config-only
    callers (decoy validate) catch a bad artifact before a long run.
    """
    from decoy_engine.generation.statistical import load_spec
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        seen: list[str] = []
        for col_entry in table_entry.get("generate_columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            col_name = col_entry.get("name", "?")
            if col_entry.get("type") == "statistical":
                try:
                    spec = load_spec(col_entry)
                except StatisticalSpecError as exc:
                    raise PlanCompileError(
                        code=exc.code,
                        path=f"tables.{table_name}.generate_columns.{col_name}",
                        message=exc.message,
                    ) from exc
                if spec.condition_on is not None and spec.condition_on not in seen:
                    raise PlanCompileError(
                        code="statistical_condition_column_unavailable",
                        path=f"tables.{table_name}.generate_columns.{col_name}.condition_on",
                        message=(
                            f"statistical column {col_name!r} conditions on "
                            f"{spec.condition_on!r}, which is not declared earlier in "
                            f"table {table_name!r}'s generate_columns. Sequential "
                            f"conditional sampling needs the parent first."
                        ),
                    )
            seen.append(str(col_name))


def check_text_redact_ner_available(config: dict[str, Any]) -> None:
    """Reject `text_redact` columns whose `ner` config cannot run here.

    Compile-check ownership table row #13 (capability-gaps WS2,
    2026-06-12). NER is an optional capability (the `ner` extra plus a
    separately-downloaded spaCy model); a column that opts in while
    either piece is missing is guaranteed dead at run. Config + installed
    packages only (no model load, no profile): safe for config-only
    callers (decoy validate).
    """
    from decoy_engine.storm.ner import DEFAULT_NER_MODEL, NerUnavailableError, ensure_ner_available

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "text_redact":
                continue
            provider_config = col_entry.get("provider_config") or {}
            ner_cfg = provider_config.get("ner") if isinstance(provider_config, dict) else None
            if not ner_cfg:
                continue
            model = DEFAULT_NER_MODEL
            if isinstance(ner_cfg, dict) and ner_cfg.get("model"):
                model = str(ner_cfg["model"])
            col_name = col_entry.get("name", "?")
            try:
                ensure_ner_available(model)
            except NerUnavailableError as exc:
                raise PlanCompileError(
                    code=exc.code,
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.ner",
                    message=exc.message,
                ) from exc


def check_vault_columns(config: dict[str, Any]) -> None:
    """Reject `vault: true` columns whose vault entries could not work.

    Compile-check ownership table row #14 (deferred follow-up 1,
    2026-06-12). Two structural rules:

    - a vaulted column needs a `namespace`: the vault's lookup key is
      `(namespace, masked_value)`, so without one the entry could never
      be found at unmask time;
    - `vault: true` on `strategy: fpe` is rejected: fpe is already
      algebraically reversible under the config's seed, so a vault there
      stores a second copy of the source values for zero capability,
      pure disclosure liability.

    Config-only (no profile, no source data), so it runs in both
    compile branches and in `run_config_only_checks`.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict) or not col_entry.get("vault"):
                continue
            col_name = col_entry.get("name", "?")
            if col_entry.get("strategy") == "fpe":
                raise PlanCompileError(
                    code="vault_strategy_reversible",
                    path=f"tables.{table_name}.columns.{col_name}.vault",
                    message=(
                        f"column {col_name!r} in table {table_name!r} declares "
                        "vault: true on strategy fpe, which `decoy unmask` already "
                        "reverses from the config alone. A vault there duplicates "
                        "the source values for no capability; remove the flag."
                    ),
                )
            if not col_entry.get("namespace"):
                raise PlanCompileError(
                    code="vault_requires_namespace",
                    path=f"tables.{table_name}.columns.{col_name}.vault",
                    message=(
                        f"column {col_name!r} in table {table_name!r} declares "
                        "vault: true but has no namespace; vault entries are keyed "
                        "by (namespace, masked_value), so add a namespace."
                    ),
                )
