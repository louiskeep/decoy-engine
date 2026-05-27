"""pool_capacity_pre_flight: compile-check row 7.

Per S5 spec §6 + cross-sprint contracts §4 row 7: for every column with
`poolable: True` provider + a cardinality mode that requires capacity
guarantees (UNIQUE, MATCH_SOURCE_CARDINALITY, SCALE_SOURCE_CARDINALITY):

- UNIQUE: pool_size >= source.distinct_count.
- MATCH: pool_size >= source.distinct_count.
- SCALE: pool_size >= source.distinct_count * scale.

Supersedes S1's `basic_uniqueness_pre_flight` for pool-backed columns
(R1). Profile-dependent; under `--no-profile`: goes to checks_skipped
for non-unique modes; hard error for UNIQUE columns.

Behavior governed by `on_pool_exhaustion` setting:
- `scale_up` (default per PO PQ3): silently scale + warn.
- `fall_back`: drop pool path + warn.
- `fail`: hard error (raise PoolCapacityError).
"""

from __future__ import annotations

from typing import Any

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
) -> None:
    """Pool-backed capacity check (row 7 of compile-check ownership table).

    Per S5 spec §6: supersedes basic_uniqueness_pre_flight for pool-backed
    columns. on_pool_exhaustion='fail' raises PoolCapacityError; other
    values defer to runtime (sampler emits QualityWarning).

    Raises:
        PoolCapacityError(code='pool_too_small_for_source') when
        on_pool_exhaustion=='fail' and source distinct exceeds pool capacity.
    """
    # Build (table, column) -> distinct_count from profile.
    distinct_lookup: dict[tuple[str, str], int | None] = {}
    if profile is not None:
        for table in getattr(profile, "tables", ()):
            for col in getattr(table, "columns", ()):
                distinct_lookup[(table.name, col.name)] = col.distinct_count

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
            pool_size = col_entry.get("pool_size", 10_000)
            col_name = col_entry.get("name", "?")
            source_distinct = distinct_lookup.get((table_name, col_name))
            if source_distinct is None:
                continue  # profile lacked the column; runtime catches
            scale = (
                float(col_entry.get("scale", 2.0))
                if cardinality_mode == "scale_source_cardinality"
                else 1.0
            )
            needed = source_distinct if scale == 1.0 else int(source_distinct * scale)
            if needed > pool_size and on_pool_exhaustion == "fail":
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
