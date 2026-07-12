"""pool_capacity_pre_flight: compile-check row 7.

Per S5 spec §6 + cross-sprint contracts §4 row 7: for every column with
`poolable: True` provider + a cardinality mode that requires capacity
guarantees (UNIQUE, MATCH_SOURCE_CARDINALITY, SCALE_SOURCE_CARDINALITY):

- UNIQUE: pool_size >= non-null output row count (DE-11). UNIQUE draws one
  distinct value per non-null output row, so capacity is keyed on the rows the
  sampler fills, NOT on the source distinct count. Checked via the SAME
  `unique_capacity_ok` the runtime sampler uses, so the two cannot disagree.
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

from decoy_engine.generation.pool._capacity import resolve_pool_size, unique_capacity_ok
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
    # Build (table, column) -> distinct_count from profile. Under --no-profile
    # the distinct counts are unavailable / untrusted, so the lookup stays
    # empty and every column reads as "distinct unknown".
    distinct_lookup: dict[tuple[str, str], int | None] = {}
    # DE-11: UNIQUE feasibility is keyed on the NON-NULL OUTPUT ROW COUNT
    # (row_count - null_count) -- the number of distinct values the sampler
    # must actually draw -- NOT the source distinct count. Same profile
    # availability guard as distinct_lookup: under --no-profile the counts are
    # untrusted, so the lookup stays empty and UNIQUE reads as "unverifiable".
    nonnull_lookup: dict[tuple[str, str], int | None] = {}
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
            # DE-11: resolve pool_size from the ONE canonical location shared
            # with the seed-envelope builder + faker handler (top-level
            # pool_size, else provider_config.pool_size, else default).
            pool_size, _pool_size_declared = resolve_pool_size(col_entry)
            col_name = col_entry.get("name", "?")
            source_distinct = distinct_lookup.get((table_name, col_name))

            if cardinality_mode == "unique":
                # Uniqueness is a correctness contract: hard-error whenever it
                # cannot be proven, independent of on_pool_exhaustion (F3) and
                # of profile availability (F4). DE-11: feasibility is keyed on
                # the NON-NULL OUTPUT ROW COUNT (what the sampler draws), via
                # the SAME unique_capacity_ok() the runtime sampler uses.
                nonnull_output_rows = nonnull_lookup.get((table_name, col_name))
                if nonnull_output_rows is None:
                    raise PoolCapacityError(
                        code="pool_capacity_unverifiable_no_profile",
                        message=(
                            f"Column {table_name}.{col_name} uses cardinality_mode='unique' "
                            "but no output row count is available "
                            f"({'compile ran with --no-profile' if no_profile else 'profile lacks the column'}). "
                            "The pool cannot be proven large enough to supply unique values, "
                            "and uniqueness cannot be deferred to runtime. Profile the source, "
                            "or pick a non-unique cardinality_mode."
                        ),
                    )
                if not unique_capacity_ok(pool_size, nonnull_output_rows):
                    raise PoolCapacityError(
                        code="pool_too_small_for_source",
                        message=(
                            f"Column {table_name}.{col_name} uses cardinality_mode='unique' "
                            f"with pool_size={pool_size}, but the job produces "
                            f"{nonnull_output_rows} non-null output rows, each of which needs a "
                            "distinct pool value. The pool cannot supply enough unique values. "
                            "Raise pool_size or pick a non-unique cardinality_mode."
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
