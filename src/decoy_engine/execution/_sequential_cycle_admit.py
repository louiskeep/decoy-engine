"""Admission gate and memory pricing for cross-table FK cycles on the sequential (SEQ) route.

A cyclic component is admitted only when every condition below holds. Each one is decided from
the plan, the relationship graph, the job's config-level facts and table sizes, never from row
data, and each decline carries a stable code and a message that names schema identifiers only.

Why the producer set is a default-deny allowlist. Pass 1 (`_sequential_cycle`) builds the
effective key maps from a key-columns-only read. That reproduces the full-frame maps only if the
key's masked value is a pure function of the key's own source value. `hash`, `passthrough`,
`redact`, `truncate` and `fpe` are; `derived` reads payload siblings, positional strategies depend
on row order, `when:` makes the output depend on a predicate over other columns, and a `joint_mask`
or composite entry can overwrite the key from another column's entry. The check therefore runs
against EVERY writer of a cyclic key column (`_column_access.column_access`, the same declaration
the chunked carry plan trusts), and an undeclared writer blocks the table.

Why memory is priced per allocation. Admission follows one rule: price every live allocation or
decline. The terms are the retained parent maps, the per-cell error caches, the key-only scratch
of pass 1, the largest table's working set (frame plus its Arrow output, which are both live at the
table transition) and the strategy state the run keeps outside any table (the `PoolCache`). `code_set`
pins whole corpus records outside the cache and has no bound the config states, so it declines.
The per-entry byte constants are upper-bound estimates of CPython dict and tuple overhead, not
calibrated measurements; an unavailable size or budget declines instead of being assumed to fit.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._column_access import column_access, has_when
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list, order_work, provider_is_composite
from decoy_engine.execution._sequential_cycle import cyclic_key_columns, fk_components
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

WORK_GRAPH_CYCLIC = "sequential_cycle_work_graph_cyclic"
KEY_STRATEGY = "sequential_cycle_key_strategy_unsupported"
KEY_WHEN = "sequential_cycle_key_when_unsupported"
KEY_WRITER = "sequential_cycle_key_overlapping_writer"
TRANSFORMS = "sequential_cycle_table_transforms"
QUARANTINE = "sequential_cycle_quarantine_enabled"
PAYLOAD_UNPRICED = "sequential_cycle_payload_state_unpriced"
PAYLOAD_OVER_BUDGET = "sequential_cycle_payload_state_over_budget"
BUDGET_EXCEEDED = "sequential_cycle_budget_exceeded"
BUDGET_UNAVAILABLE = "sequential_cycle_budget_unavailable"
PROJECTED_READ = "sequential_cycle_projected_read_unavailable"

# Pure functions of the key's own source value, checked equal on full column and key-only read,
# duplicates included. Everything else is declined.
KEY_PRODUCER_ALLOWLIST = frozenset({"hash", "passthrough", "redact", "truncate", "fpe"})
_POOL_STRATEGIES = frozenset({"faker", "nested"})
# `composite` entries build a per-node bundle pool sized by config (up to 2**56 entries) that
# never goes through the PoolCache, so its bytes have no bound the cache capacity states.
_UNPRICED_STRATEGIES = frozenset({"code_set", "composite"})

# Upper-bound CPython costs, per live entry. See the module docstring.
_MAP_ENTRY_BASE, _MAP_ENTRY_PER_COLUMN = 216, 176
_ERROR_ENTRY_BASE, _ERROR_ENTRY_PER_COLUMN = 168, 224
_NARROW_CELL_BYTES = 200
# Arrow bytes grow when pandas holds strings as Python objects.
PANDAS_EXPANSION = 3


@dataclass(frozen=True)
class CycleDecline:
    """Why a cyclic component cannot run on the SEQ route. `message` names no row data."""

    code: str
    message: str


@dataclass(frozen=True)
class CycleContext:
    """Job facts the gate needs beyond the plan: sizes, budget, config-level flags, the read.

    `projected_loader(table, columns)` returns only `columns` of `table`; None means the source
    can only be read whole, which the key-only pass must not do. `table_rows` and `table_bytes`
    cover every table of the job (bytes are the pandas-side working size of the whole table).
    A missing size or a None budget declines the job."""

    budget_bytes: int | None
    table_rows: Mapping[str, int]
    table_bytes: Mapping[str, int]
    transform_tables: frozenset[str] = frozenset()
    projected_loader: Callable[[str, Sequence[str]], pa.Table] | None = None


@dataclass(frozen=True)
class CyclePrice:
    """Every live allocation of a cyclic job at its peak, in bytes."""

    parent_maps_bytes: int
    error_cache_bytes: int
    narrow_scratch_bytes: int
    working_set_bytes: int
    payload_state_bytes: int

    @property
    def total(self) -> int:
        return (
            self.parent_maps_bytes
            + self.error_cache_bytes
            + self.narrow_scratch_bytes
            + self.working_set_bytes
            + self.payload_state_bytes
        )


def default_pool_capacity_bytes() -> int:
    """Capacity of the `PoolCache` a run builds when the caller passes none."""
    from decoy_engine.generation.pool._cache import PoolCache

    return PoolCache().stats().bytes_capacity


def _entry(name: str, seed: ColumnSeed) -> dict[str, Any]:
    """A plan seed as the config-entry dict `column_access` declares access for."""
    return {
        "name": name,
        "strategy": seed.strategy,
        "provider": seed.provider,
        "provider_config": provider_config_to_dict(seed.provider_config),
        "coherent_with": list(seed.coherent_with),
        "when": seed.when,
    }


def _quarantine_enabled(quarantine_config: Mapping[str, Any] | None) -> bool:
    cfg = quarantine_config or {}
    return bool(cfg.get("enabled", False)) and bool(cfg.get("triggers"))


def _work_graph_decline(
    plan: Plan, graph: RelationshipGraph, registry: ProviderRegistry
) -> CycleDecline | None:
    try:
        order_work(build_work_list(plan, registry), graph)
    except ExecutionError:
        return CycleDecline(
            WORK_GRAPH_CYCLIC,
            "the cross-table foreign-key cycle is also a cycle between column work nodes, so no "
            "column order exists for the sequential route; use execution_mode='full_frame'.",
        )
    return None


def _own_entry_decline(
    table: str, column: str, own: ColumnSeed, registry: ProviderRegistry
) -> CycleDecline | None:
    """The key's own declaration must be an allowlisted pure function of the key value."""
    where = f"{table}.{column}"
    entry = _entry(column, own)
    if has_when(entry):
        return CycleDecline(
            KEY_WHEN,
            f"cyclic key column {where} is gated by 'when:'; the sequential route "
            "reads the key column alone and cannot evaluate the gate.",
        )
    access = column_access(entry, registry)
    if (
        own.strategy not in KEY_PRODUCER_ALLOWLIST
        or provider_is_composite(own.provider, registry)
        or access.reads
        or access.reads_unknown
        or access.writes - {column}
        or access.writes_unknown
    ):
        return CycleDecline(
            KEY_STRATEGY,
            f"cyclic key column {where} uses strategy {own.strategy!r}; the sequential route "
            "admits only hash, passthrough, redact, truncate and fpe keys that read no other column.",
        )
    return None


def _key_decline(
    plan: Plan,
    graph: RelationshipGraph,
    registry: ProviderRegistry,
    components: list[tuple[str, ...]],
) -> CycleDecline | None:
    keys: dict[str, set[str]] = {}
    for comp in components:
        for table, cols in cyclic_key_columns(graph, comp).items():
            keys.setdefault(table, set()).update(cols)
    declared = {t: dict(seed.per_column) for t, seed in plan.seed_envelope.per_table}
    for table, columns in sorted(keys.items()):
        entries = declared.get(table, {})
        for column in sorted(columns):
            if column in entries:
                found = _own_entry_decline(table, column, entries[column], registry)
                if found is not None:
                    return found
    for table, columns in sorted(keys.items()):
        entries = declared.get(table, {})
        for column in sorted(columns):
            for other, other_seed in entries.items():
                if other == column:
                    continue
                access = column_access(_entry(other, other_seed), registry)
                if column in access.writes or access.writes_unknown:
                    return CycleDecline(
                        KEY_WRITER,
                        f"cyclic key column {table}.{column} can also be written by column "
                        f"{table}.{other}; the sequential route needs exactly one writer per key.",
                    )
    return None


def _all_tables(plan: Plan, graph: RelationshipGraph) -> list[str]:
    seen: dict[str, None] = {}
    for name, _ in plan.seed_envelope.per_table:
        seen.setdefault(name)
    for e in graph.edges:
        seen.setdefault(e.parent_table)
        seen.setdefault(e.child_table)
    return list(seen)


def _strategies(plan: Plan, registry: ProviderRegistry) -> Iterable[tuple[str, ColumnSeed]]:
    for _, table_seed in plan.seed_envelope.per_table:
        for _, seed in table_seed.per_column:
            yield (
                ("composite" if provider_is_composite(seed.provider, registry) else seed.strategy),
                seed,
            )


def price_cycle_job(
    plan: Plan,
    graph: RelationshipGraph,
    registry: ProviderRegistry,
    ctx: CycleContext,
    *,
    pool_capacity_bytes: int,
) -> CyclePrice | None:
    """The peak of every live allocation, or None when a size the price needs is unknown.

    Every parent map is counted as live at once (the upper bound of the release schedule), each
    with an error cache sized for the worst case of one errored cell per row."""
    tables = _all_tables(plan, graph)
    if any(t not in ctx.table_rows or t not in ctx.table_bytes for t in tables):
        return None
    maps = error = 0
    for parent, columns in {(e.parent_table, e.parent_columns) for e in graph.edges}:
        n = len(columns)
        maps += ctx.table_rows[parent] * (_MAP_ENTRY_BASE + _MAP_ENTRY_PER_COLUMN * n)
        error += ctx.table_rows[parent] * (_ERROR_ENTRY_BASE + _ERROR_ENTRY_PER_COLUMN * n)
    narrow = sum(
        ctx.table_rows[t] * len(cols) * _NARROW_CELL_BYTES
        for comp in fk_components(graph)
        for t, cols in cyclic_key_columns(graph, comp).items()
    )
    working = 2 * max(ctx.table_bytes[t] for t in tables)
    pooled = any(strategy in _POOL_STRATEGIES for strategy, _ in _strategies(plan, registry))
    return CyclePrice(maps, error, narrow, working, pool_capacity_bytes if pooled else 0)


def cycle_decline(
    plan: Plan,
    graph: RelationshipGraph,
    registry: ProviderRegistry,
    ctx: CycleContext,
    *,
    quarantine_config: Mapping[str, Any] | None = None,
    pool_capacity_bytes: int | None = None,
    structural_only: bool = False,
) -> CycleDecline | None:
    """None when the graph has no cross-table cycle or every cyclic component is admitted.

    `structural_only` skips the checks that depend on the sources (the column read and the memory price). The transforms
    preflight runs before transformed tables exist, so it may reject only what no transform
    can change; routing re-runs the full gate on the prepared sources."""
    components = fk_components(graph)
    if not components:
        return None
    if _quarantine_enabled(quarantine_config):
        return CycleDecline(
            QUARANTINE,
            "row-error quarantine is enabled for this job; it would keep whole masked rows from "
            "every table in memory, so a cross-table cycle cannot run on the sequential route.",
        )
    in_cycle = {t for comp in components for t in comp}
    if ctx.transform_tables & in_cycle:
        return CycleDecline(
            TRANSFORMS,
            "a table in the cross-table cycle declares transforms, which the key-only pass "
            "cannot reproduce; apply them upstream or use execution_mode='full_frame'.",
        )
    found = _work_graph_decline(plan, graph, registry) or _key_decline(
        plan, graph, registry, components
    )
    if found is not None:
        return found
    if any(s in _UNPRICED_STRATEGIES for s, _ in _strategies(plan, registry)):
        return CycleDecline(
            PAYLOAD_UNPRICED,
            "a code_set or composite column keeps strategy state outside the bounded PoolCache "
            "(a whole corpus, or a config-sized bundle pool), so the sequential route cannot "
            "price the cyclic job.",
        )
    if structural_only:
        return None
    if ctx.projected_loader is None:
        return CycleDecline(
            PROJECTED_READ,
            "the source cannot be read by column, and the sequential route will not read a whole "
            "cyclic table to build the key maps.",
        )
    pool = default_pool_capacity_bytes() if pool_capacity_bytes is None else pool_capacity_bytes
    price = price_cycle_job(plan, graph, registry, ctx, pool_capacity_bytes=pool)
    if price is None or ctx.budget_bytes is None:
        return CycleDecline(
            BUDGET_UNAVAILABLE,
            "a table size or the memory budget is unknown, so the sequential route cannot show "
            "the cyclic job fits; it declines instead of assuming it does.",
        )
    if price.payload_state_bytes > ctx.budget_bytes:
        return CycleDecline(
            PAYLOAD_OVER_BUDGET,
            f"the strategy state the run keeps resident ({price.payload_state_bytes} bytes) "
            f"exceeds the memory budget ({ctx.budget_bytes} bytes).",
        )
    if price.total > ctx.budget_bytes:
        return CycleDecline(
            BUDGET_EXCEEDED,
            f"the combined live state of the cyclic job ({price.total} bytes) exceeds the memory "
            f"budget ({ctx.budget_bytes} bytes).",
        )
    return None


def require_cycle_admitted(
    plan: Plan,
    graph: RelationshipGraph,
    registry: ProviderRegistry,
    ctx: CycleContext | None,
    *,
    quarantine_config: Mapping[str, Any] | None = None,
    pool_capacity_bytes: int | None = None,
) -> None:
    """Raise the coded decline for a cyclic job the SEQ route cannot take; a no-op otherwise."""
    if not fk_components(graph):
        return
    if ctx is None:
        found: CycleDecline | None = CycleDecline(
            BUDGET_UNAVAILABLE,
            "this job has a cross-table foreign-key cycle and no sizing context, so the "
            "sequential route cannot show it fits; run it through run_pipeline.",
        )
    else:
        found = cycle_decline(
            plan,
            graph,
            registry,
            ctx,
            quarantine_config=quarantine_config,
            pool_capacity_bytes=pool_capacity_bytes,
        )
    if found is not None:
        raise ExecutionError(code=found.code, message=found.message)


def _source_bytes(source: pa.Table | LazySource) -> int:
    """Arrow bytes of a resident table, or the Parquet footer's row-group byte total."""
    if isinstance(source, LazySource):
        meta = pq.read_metadata(source.path)
        return sum(meta.row_group(i).total_byte_size for i in range(meta.num_row_groups))
    return int(source.nbytes)


def cycle_context_from_run(
    plan: Plan,
    graph: RelationshipGraph,
    config: Mapping[str, Any],
    caller_sources: Mapping[str, pa.Table | LazySource],
    out_of_core_budget_bytes: int | None,
) -> CycleContext:
    """The gate's context for a `run_pipeline` job. A table the caller did not hand over as a
    resident table or Parquet handle has no size and no column read, so it declines."""
    from decoy_engine.execution._transforms_gate import transform_bearing_mask_tables
    from decoy_engine.execution.out_of_core import resolve_budget

    try:
        budget: int | None = resolve_budget(out_of_core_budget_bytes).budget_bytes
    except ExecutionError:
        budget = None
    tables = _all_tables(plan, graph)
    rows = {t: caller_sources[t].num_rows for t in tables if t in caller_sources}
    sizes = {
        t: PANDAS_EXPANSION * _source_bytes(caller_sources[t])
        for t in tables
        if t in caller_sources
    }

    def read_columns(table: str, columns: Sequence[str]) -> pa.Table:
        source = caller_sources[table]
        try:
            if isinstance(source, LazySource):
                return source.read_columns(columns)
            return source.select(list(columns))
        except (pa.ArrowInvalid, KeyError, ValueError):
            # pyarrow's own message can quote the file path; name only schema identifiers.
            raise ExecutionError(
                code=PROJECTED_READ,
                message=(
                    f"table {table!r} has no top-level column among {sorted(columns)!r} that the "
                    "sequential cycle pass can read by name; a missing or nested key column "
                    "cannot be projected."
                ),
            ) from None

    complete = all(t in caller_sources for t in tables)
    return CycleContext(
        budget_bytes=budget,
        table_rows=rows,
        table_bytes=sizes,
        transform_tables=transform_bearing_mask_tables(config),
        projected_loader=read_columns if complete else None,
    )


def route_cycle_decline(
    plan: Plan,
    graph: RelationshipGraph,
    registry: ProviderRegistry,
    config: Mapping[str, Any],
    caller_sources: Mapping[str, pa.Table | LazySource],
    out_of_core_budget_bytes: int | None,
    *,
    structural_only: bool = False,
) -> CycleDecline | None:
    """`run_pipeline`'s routing-time gate: the decline for this job's cycle, None when admitted
    or when the graph has no cross-table cycle."""
    if not fk_components(graph):
        return None
    ctx = cycle_context_from_run(plan, graph, config, caller_sources, out_of_core_budget_bytes)
    return cycle_decline(
        plan,
        graph,
        registry,
        ctx,
        quarantine_config=config.get("quarantine"),
        structural_only=structural_only,
    )
