"""Independent multi-table dispatch for `run_pipeline` (Rust engine program slice B7).

A job with several mask tables used to run as one full-frame pandas call whatever the
tables' sizes. This module lets each table take the route it would take alone: a table
that would auto-chunk as a single-table job runs through B2's dispatcher lane
(`_pipeline_auto_chunk.run_auto_chunk`), one table at a time, and every other table runs
in one full-frame adapter call, exactly as before.

Two tables are independent when no FK edge touches the job: the only cross-table data read
during masking is the FK machinery, and every other per-job structure is keyed by table.
Jobs the split cannot reproduce faithfully stay whole (quarantine, a vault writer,
validators, a shuffle that draws without a seed, a non-deterministic categorical whose
positional split is deferred). The plan is
docs/plans/2026-10-01-multi-table-dispatch.md; the per-unit fallback with a recorded reason
follows the pattern Apache Gluten and Spark RAPIDS use for a partly accelerated plan.

Each unit builds its own pools and loads its own code-set corpora. That is safe because
pool and corpus identities are deterministic functions of config and seed (the S5 F2
deterministic pool-build contract), so the same entry built in two units, or in a
different order than the full-frame call used, is the same entry.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution import _chunked_input, _pipeline_auto_chunk
from decoy_engine.execution import _chunked_output_sink as _output_sink
from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionAdapter
    from decoy_engine.execution._chunked_input import SourceFacts
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph

__all__ = [
    "POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED",
    "UNSEEDED_RANDOM_STRATEGIES",
    "MultiTableSplit",
    "decide_multi_table_split",
    "position_keyed_deferred_nodes",
    "run_multi_table_split",
    "split_reproducibility_stamp",
    "unseeded_random_nodes",
]

_LOG = logging.getLogger(__name__)

# Strategies with a mode that draws from a fresh `default_rng()` per call, so two
# invocations differ. `nested` is checked through its child strategy.
UNSEEDED_RANDOM_STRATEGIES = frozenset({"shuffle"})

# Non-deterministic categorical is seeded and keyed by the handler-frame ordinal, so it is
# reproducible. A split would hand it per-table frames whose ordinals the whole-frame call
# never saw, and the split path has no positional implementation yet, so a table that
# carries one stays whole (siblings too). Remove this veto when the split path keys by
# durable row position.
POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED = frozenset({"categorical"})


@dataclasses.dataclass(frozen=True)
class MultiTableSplit:
    """Which mask tables dispatch and which stay in the full-frame group.

    `reasons` has one entry per mask table, in config order: the single-table chunked
    reason for a dispatched table, the single-table rejection for the others."""

    dispatched: tuple[str, ...]
    full_frame: tuple[str, ...]
    reasons: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))


def _non_deterministic_nodes(
    plan: Plan, strategies: frozenset[str]
) -> tuple[tuple[str, str, str], ...]:
    """`(table, column, strategy)` of every non-deterministic column whose strategy, or whose
    `nested` child strategy, is in `strategies`. Reads the plan only, so a `when:`-gated
    column counts whether or not any row matches."""
    found: list[tuple[str, str, str]] = []
    for table, table_seed in plan.seed_envelope.per_table:
        for column, seed in table_seed.per_column:
            if seed.deterministic:
                continue
            strategy = seed.strategy
            if strategy == "nested":
                child = provider_config_to_dict(seed.provider_config).get("strategy")
                if child in strategies:
                    found.append((table, column, strategy))
            elif strategy in strategies:
                found.append((table, column, strategy))
    return tuple(found)


def unseeded_random_nodes(plan: Plan) -> tuple[tuple[str, str, str], ...]:
    """`(table, column, strategy)` of every plan column that masks from an unseeded generator."""
    return _non_deterministic_nodes(plan, UNSEEDED_RANDOM_STRATEGIES)


def position_keyed_deferred_nodes(plan: Plan) -> tuple[tuple[str, str, str], ...]:
    """`(table, column, strategy)` of every column whose seeded, position-keyed draw has no
    split implementation yet (`POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED`)."""
    return _non_deterministic_nodes(plan, POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED)


def _job_gates_hold(
    config: dict[str, Any],
    *,
    plan: Plan,
    graph: RelationshipGraph,
    substrate: str,
    table_kinds: Mapping[str, str],
    auto_chunk: bool,
    dispatcher_enabled: bool,
    split_enabled: bool,
    vault_writer_present: bool,
) -> bool:
    mask_tables = [name for name, kind in table_kinds.items() if kind == "mask"]
    return (
        auto_chunk
        and dispatcher_enabled
        and split_enabled
        and len(mask_tables) >= 2
        and len(mask_tables) == len(table_kinds)
        and not graph.edges
        and not config.get("relationships")
        and substrate == "pandas"
        and not (config.get("quarantine") or {}).get("enabled")
        and not vault_writer_present
        and not config.get("validators")
        and not unseeded_random_nodes(plan)
        and not position_keyed_deferred_nodes(plan)
    )


def decide_multi_table_split(
    config: dict[str, Any],
    *,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    substrate: str,
    caller_sources: Mapping[str, Any],
    table_kinds: Mapping[str, str],
    auto_chunk: bool,
    auto_chunk_threshold_rows: int,
    dispatcher_enabled: bool,
    split_enabled: bool,
    vault_writer_present: bool,
    source_facts: Mapping[str, SourceFacts] | None = None,
) -> MultiTableSplit | None:
    """The per-table split of a multi-table job, or `None` when the job stays whole.

    Read-only: it reads config, the plan, source schemas and Arrow row and null counts (a
    `LazySource`'s from the routing snapshot `source_facts`, never a second footer read).
    A table dispatches exactly when the planner would route it chunked as a single-table
    job with its own source, so each table is classified by the planner itself on the job
    restricted to that table. Passing only that table's source keeps sibling frames out of
    the planner's "extra loaded frames" check while every other runtime gate applies."""
    if not _job_gates_hold(
        config,
        plan=plan,
        graph=graph,
        substrate=substrate,
        table_kinds=table_kinds,
        auto_chunk=auto_chunk,
        dispatcher_enabled=dispatcher_enabled,
        split_enabled=split_enabled,
        vault_writer_present=vault_writer_present,
    ):
        return None
    from decoy_engine.execution._planner import classify_job

    dispatched: list[str] = []
    full_frame: list[str] = []
    reasons: dict[str, str] = {}
    for table in (name for name, kind in table_kinds.items() if kind == "mask"):
        alone = {**config, "tables": [t for t in config["tables"] if t.get("name") == table]}
        decision = classify_job(
            alone,
            plan=plan,
            registry=registry,
            relationship_graph=graph,
            substrate=substrate,
            source_tables={table: caller_sources[table]} if table in caller_sources else {},
            auto_chunk_threshold_rows=auto_chunk_threshold_rows,
            source_facts=source_facts,
        )
        if decision.mode == "chunked":
            dispatched.append(table)
            reasons[table] = decision.reason
        else:
            full_frame.append(table)
            reasons[table] = decision.rejections["chunked"]
    if not dispatched:
        return None
    return MultiTableSplit(tuple(dispatched), tuple(full_frame), reasons)


def split_reproducibility_stamp(
    split: MultiTableSplit, *, chunk_size_rows: int, auto_chunk_threshold_rows: int
) -> dict[str, Any]:
    """The six reproducibility keys of `quality_metrics["auto_chunk"]` for a split call.

    `source_rows` and `chunk_count` are `null`, as for any multi-table job; the per-table
    numbers sit in the block's `tables` list."""
    names = ", ".join(split.dispatched)
    return {
        "mode": "chunked",
        "chunk_size_rows": chunk_size_rows,
        "threshold_rows": auto_chunk_threshold_rows,
        "source_rows": None,
        "chunk_count": None,
        "reason": (
            f"multi_table_split: {len(split.dispatched)} of {len(split.reasons)} "
            f"mask tables dispatched ({names})"
        ),
    }


def _table_entry(
    split: MultiTableSplit,
    table: str,
    resident_sources: Mapping[str, Any],
    chunk_size_rows: int,
    lane_block: Mapping[str, Any] | None,
) -> dict[str, Any]:
    rows = resident_sources[table].num_rows if table in resident_sources else None
    dispatched = table in split.dispatched
    entry: dict[str, Any] = {
        "table": table,
        "dispatched": dispatched,
        "source_rows": rows,
        "chunk_count": -(-rows // chunk_size_rows) if dispatched and rows is not None else None,
        "reason": split.reasons[table],
    }
    if lane_block is not None:
        entry.update({k: lane_block[k] for k in ("lane", "lane_reason", "native_threads")})
        for key in ("output", "input"):
            if key in lane_block:
                entry[key] = lane_block[key]
    return entry


def run_multi_table_split(
    split: MultiTableSplit,
    config: dict[str, Any],
    *,
    resident_sources: Mapping[str, Any],
    engine_version: str,
    registry: ProviderRegistry,
    adapter: ExecutionAdapter,
    chunk_size_rows: int,
    key_provider: KeyProvider | None,
    native_threads: int,
    plan: Plan,
    graph: RelationshipGraph,
    namespace_registry: NamespaceRegistry,
    unconfigured_column_policy: UnconfiguredColumnPolicy,
    generate_output_tables: frozenset[str],
    sink: TransactionalSink | None = None,
    output_reason: str | None = None,
    input_facts: Mapping[str, SourceFacts] | None = None,
    input_reasons: Mapping[str, str] | None = None,
) -> tuple[
    dict[str, pa.Table], tuple[Any, ...], float, tuple[Any, ...], dict[str, Any], tuple[Any, ...]
]:
    """Run the dispatched tables one at a time in config order, then the group once.

    The first unit that raises ends the call and no later unit starts. A dispatched
    table is masked by `run_auto_chunk`, looked up through its module so a spy sees every
    call; its output is kept as `run_auto_chunk` returned it, and the chunk list stays
    local to that call. Returns `(outputs, timings, boundary_conversion_ms, warnings,
    quality_metrics, row_errors)`.

    With a `sink` (B6a; the caller verified every output is dispatched) each dispatched
    table streams into it and the returned outputs are `{}`; `output_reason` names why a
    run without a sink stays resident, recorded per dispatched table. A dispatched `LazySource`
    (B6b; only with a sink) is read as batches and compared with its routing snapshot in
    `input_facts`; `input_reasons` names why a table that was lazy is resident, per table."""
    if sink is not None and (
        split.full_frame or any(name not in split.dispatched for name in resident_sources)
    ):
        # B6a: never run the full-frame adapter, or mask a table, while a publish session
        # may be open; `decide_output_mode` declines these splits, this refuses them too.
        raise ExecutionError(
            code="split_sink_needs_all_dispatched",
            message="a streamed multi-table split needs every output table dispatched.",
        )
    _LOG.info(
        "multi-table split dispatched=%s full_frame=%s",
        ", ".join(split.dispatched),
        ", ".join(split.full_frame) or "none",
    )
    dispatched_out: dict[str, pa.Table] = {}
    dispatched_names: list[str] = []
    timings: list[Any] = []
    warnings: list[Any] = []
    conversion_ms = 0.0
    corpora: list[Any] = []
    route_by_table: dict[str, Any] = {}
    lane_blocks: dict[str, Mapping[str, Any]] = {}
    for table in split.dispatched:
        lane_extra: dict[str, Any] = (
            {} if sink is None else {"sink": sink, "expected": (input_facts or {}).get(table)}
        )
        out, unit_timings, unit_ms, unit_warnings, unit_metrics = (
            _pipeline_auto_chunk.run_auto_chunk(
                config,
                resident_sources[table],
                table=table,
                engine_version=engine_version,
                registry=registry,
                adapter=adapter,
                vault_writer=None,
                chunk_size_rows=chunk_size_rows,
                key_provider=key_provider,
                native_threads=native_threads,
                dispatcher_enabled=True,
                **lane_extra,
            )
        )
        dispatched_names.append(table)
        if sink is None:
            dispatched_out[table] = out[table]
        timings.extend(unit_timings)
        warnings.extend(unit_warnings)
        conversion_ms += unit_ms
        corpora.extend(unit_metrics.get("code_set_corpora", ()))
        route_by_table[table] = unit_metrics["chunked_route"]
        lane_blocks[table] = unit_metrics["auto_chunk"]
        if sink is None and output_reason is not None:
            lane_blocks[table] = {
                **lane_blocks[table],
                "output": _output_sink.resident_block(output_reason),
                "input": _chunked_input.resident_block(
                    (input_reasons or {}).get(table, _chunked_input.REASON_RESIDENT_SOURCE)
                ),
            }

    group_sources = {k: v for k, v in resident_sources.items() if k not in dispatched_names}
    group_outputs: dict[str, pa.Table] = {}
    group_metrics: dict[str, Any] = {}
    row_errors: tuple[Any, ...] = ()
    if group_sources:
        group = adapter.run(
            plan,
            group_sources,
            registry=registry,
            relationship_graph=graph,
            namespace_registry=namespace_registry,
            unconfigured_column_policy=unconfigured_column_policy,
            generate_output_tables=generate_output_tables,
            key_provider=key_provider,
        )
        group_outputs = dict(group.outputs)
        group_metrics = dict(group.quality_metrics)
        timings.extend(group.timings)
        warnings.extend(group.warnings)
        conversion_ms += group.boundary_conversion_ms
        corpora.extend(group_metrics.pop("code_set_corpora", ()))
        row_errors = group.row_errors

    outputs = (
        {}
        if sink is not None
        else {
            name: dispatched_out[name] if name in dispatched_out else group_outputs[name]
            for name in resident_sources
        }
    )
    quality_metrics = group_metrics
    if corpora:
        quality_metrics["code_set_corpora"] = corpora
    quality_metrics["chunked_route_by_table"] = route_by_table
    quality_metrics["auto_chunk"] = {
        "lane": _pipeline_auto_chunk.LANE_DISPATCHER,
        "lane_reason": None,
        "native_threads": native_threads,
        "tables": [
            _table_entry(split, t, resident_sources, chunk_size_rows, lane_blocks.get(t))
            for t in split.reasons
        ],
    }
    return outputs, tuple(timings), conversion_ms, tuple(warnings), quality_metrics, row_errors
