"""pool_capacity_pre_flight: compile-check row 7.

Per S5 spec §6 + cross-sprint contracts §4 row 7: for every column with
`poolable: True` provider + a cardinality mode that requires capacity
guarantees (UNIQUE, MATCH_SOURCE_CARDINALITY, SCALE_SOURCE_CARDINALITY):

- UNIQUE: pool_size >= non-null output rows (row_count - null_count) (DE-11).
- MATCH: pool_size >= source.distinct_count.
- SCALE: pool_size >= source.distinct_count * scale.

Supersedes S1's `basic_uniqueness_pre_flight` for pool-backed columns
(R1). Profile-dependent; under `--no-profile`: goes to checks_skipped
for non-unique modes; hard error for UNIQUE columns.

UNIQUE vs soft modes (S5 F3): uniqueness is a correctness contract, not a
soft-cardinality preference. UNIQUE columns hard-error whenever capacity
cannot be proven, INDEPENDENT of `on_pool_exhaustion`. The
`on_pool_exhaustion` setting governs only the soft modes
(MATCH_SOURCE_CARDINALITY / SCALE_SOURCE_CARDINALITY):
- `scale_up` (default per PO PQ3): defer + emit a deferral warning.
- `fall_back`: defer (drop pool path at runtime) + emit a deferral warning.
- `fail`: hard error (raise PoolCapacityError) at compile.

The prior implementation gated the ENTIRE check behind
`on_pool_exhaustion == 'fail'`, so a default-config `cardinality_mode:
unique` compiled cleanly and then silently violated uniqueness at runtime
by reusing pool values.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.generation.pool._capacity import unique_capacity_ok
from decoy_engine.generation.pool._errors import PoolCapacityError


def _resolve_provider_poolable(provider: str | None) -> bool:
    """Defer-import the registry; check `poolable` on the CapabilityMatrix."""
    if provider is None:
        return False
    from decoy_engine.providers_v2 import get_default_registry

    registry = get_default_registry()
    if not registry.has(provider):
        return False
    return bool(registry.get_capabilities(provider).poolable)


def check_pool_capacity_pre_flight(
    config: dict[str, Any],
    profile: Any,
    *,
    on_pool_exhaustion: str = "scale_up",
    no_profile: bool = False,
) -> tuple[str, ...]:
    """Pool-backed capacity check (row 7 of compile-check ownership table).

    Per S5 spec §6: supersedes basic_uniqueness_pre_flight for pool-backed
    columns.

    Returns a tuple of non-fatal deferral warning strings (empty when nothing
    was deferred); the planner folds these into Plan.plan_compile.warnings.

    Raises PoolCapacityError when capacity cannot be guaranteed:
        - UNIQUE mode with source distinct > pool_size: ALWAYS raises (F3),
          independent of on_pool_exhaustion.
        - UNIQUE mode under no_profile, or with no distinct count for the
          column: ALWAYS raises (code='pool_capacity_unverifiable_no_profile')
          because uniqueness cannot be proven without distinct counts and
          cannot be deferred to runtime the way soft cardinality can (F4).
        - Soft modes (MATCH/SCALE) with needed > pool_size and
          on_pool_exhaustion=='fail': raises. With scale_up/fall_back: emits a
          deferral warning instead and defers to runtime.

    Under no_profile, soft-mode capacity is unverifiable and skipped (the
    planner records `pool_capacity_pre_flight` in checks_skipped).
    """
    # Build (table, column) lookups from profile. Under --no-profile the
    # counts are unavailable / untrusted, so both stay empty and every column
    # reads as "unknown". `distinct_lookup` sizes the soft modes (MATCH/SCALE);
    # `nonnull_lookup` (row_count - null_count) sizes UNIQUE (DE-11): UNIQUE
    # needs one distinct value per non-null output row, not per source distinct
    # value.
    distinct_lookup: dict[tuple[str, str], int | None] = {}
    nonnull_lookup: dict[tuple[str, str], int] = {}
    if profile is not None and not no_profile:
        for table in getattr(profile, "tables", ()):
            for col in getattr(table, "columns", ()):
                distinct_lookup[(table.name, col.name)] = col.distinct_count
                nonnull_lookup[(table.name, col.name)] = col.row_count - col.null_count

    warnings: list[str] = []
    tables_block = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables_block:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            provider = col_entry.get("provider")
            cardinality_mode = col_entry.get("cardinality_mode", "reuse")
            if cardinality_mode not in (
                "unique",
                "match_source_cardinality",
                "scale_source_cardinality",
            ):
                continue
            if not _resolve_provider_poolable(provider):
                continue
            col_name = col_entry.get("name", "?")
            # Resolve pool_size from BOTH declaration sites (top-level +
            # provider_config) via the shared helper, the same resolver the
            # plan-layer checks use, so this capacity gate cannot drift from
            # them. A validated config dumps top-level `pool_size` as an
            # explicit None, so `.get("pool_size", 10_000)` would read None
            # (key present) and crash the capacity comparison; the 10_000
            # default belongs only when NO site declares pool_size. Deferred
            # import keeps the generation->plan edge out of module load.
            from decoy_engine.plan._pool_size import resolve_pool_size

            resolved_pool_size = resolve_pool_size(
                col_entry, table_name=table_name, col_name=col_name
            )
            pool_size = resolved_pool_size if resolved_pool_size is not None else 10_000
            source_distinct = distinct_lookup.get((table_name, col_name))

            if cardinality_mode == "unique":
                # Uniqueness is a correctness contract: hard-error whenever it
                # cannot be proven, independent of on_pool_exhaustion (F3) and
                # of profile availability (F4). DE-11: capacity is the non-null
                # output-row count (row_count - null_count), the same quantity
                # the runtime sampler draws against -- NOT the source distinct
                # count. Verifiability keys on the row/null counts, so a profile
                # with distinct_count=None but valid counts still compiles.
                nonnull_rows = nonnull_lookup.get((table_name, col_name))
                if nonnull_rows is None:
                    raise PoolCapacityError(
                        code="pool_capacity_unverifiable_no_profile",
                        message=(
                            f"Column {table_name}.{col_name} uses cardinality_mode='unique' "
                            "but no source row/null counts are available "
                            f"({'compile ran with --no-profile' if no_profile else 'profile lacks the column'}). "
                            "The pool cannot be proven large enough to supply unique values, "
                            "and uniqueness cannot be deferred to runtime. Profile the source, "
                            "or pick a non-unique cardinality_mode."
                        ),
                    )
                if not unique_capacity_ok(pool_size, nonnull_rows):
                    raise PoolCapacityError(
                        code="pool_too_small_for_source",
                        message=(
                            f"Column {table_name}.{col_name} uses cardinality_mode='unique' "
                            f"with pool_size={pool_size}, but the pool must supply one unique "
                            f"value per non-null output row ({nonnull_rows}). The pool cannot "
                            "supply enough unique values. Raise pool_size or pick a non-unique "
                            "cardinality_mode."
                        ),
                    )
                continue

            # Soft modes: MATCH_SOURCE_CARDINALITY / SCALE_SOURCE_CARDINALITY.
            if source_distinct is None:
                # Unverifiable (no_profile or profile lacked the column); soft
                # cardinality tolerates deferral, so runtime catches it.
                continue
            scale = (
                float(col_entry.get("scale", 2.0))
                if cardinality_mode == "scale_source_cardinality"
                else 1.0
            )
            needed = source_distinct if scale == 1.0 else int(source_distinct * scale)
            if needed > pool_size:
                if on_pool_exhaustion == "fail":
                    raise PoolCapacityError(
                        code="pool_too_small_for_source",
                        message=(
                            f"Column {table_name}.{col_name} uses cardinality_mode={cardinality_mode!r} "
                            f"with pool_size={pool_size}, but profile reports "
                            f"source distinct count {source_distinct} (needed >= {needed} "
                            f"with scale={scale}). Set on_pool_exhaustion='scale_up' or "
                            "'fall_back' to defer; or grow the pool."
                        ),
                    )
                # scale_up / fall_back: defer to runtime, surfacing the
                # deferral so it lands in the manifest (NF5).
                warnings.append(
                    f"pool_capacity_deferred: {table_name}.{col_name} "
                    f"(mode={cardinality_mode}, pool_size={pool_size}, needed>={needed}, "
                    f"on_pool_exhaustion={on_pool_exhaustion}); runtime will scale or fall back."
                )
    return tuple(warnings)
