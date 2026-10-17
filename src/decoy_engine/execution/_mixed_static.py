"""STATIC (pre-generation) admission of the production mixed route.

A mixed job holds generate-kind and mask-kind tables. The production route
keeps generation resident and unchanged and runs each mask table on the bounded
chunked lane, so the decision to try it is made from config, graph and source
metadata alone, before anything is generated. A decline here costs nothing: the
job continues on the existing full-frame path exactly as it did before.

The shape rules are the shared core's (`_mixed_admission`: edge topology,
complete-graph restriction, child key type). On top of them this module adds
what only the production route needs:

  * the job-level restrictions the bounded routes already enforce (validators,
    quarantine, fidelity report, post-validation, token vault, non-pandas
    substrate), which need every masked output resident at once;
  * the kill switches (`auto_chunk`, the chunked dispatcher, `mixed_route`);
  * that no relationship edge touches a mask table when there is no coupled
    edge (a mask->mask FK cannot be resolved chunk-by-chunk, and the chunked
    gate would not see it once the per-table config drops `relationships`);
  * that no mask column emits warnings whose content is whole-table scoped (counts, row
    indices) while running per chunk: such a table declines to the full-frame path, where the
    warnings equal the oracle's (`chunk_unstable_warning_column`);
  * per-mask-table chunk admission, asked of the planner's own gate
    (`classify_job`) on a single-table config so the mixed route cannot admit a
    table the pure-mask auto-chunk route would refuse.

The per-table config is the job config narrowed to one mask table, with
`relationships` removed and the coupled FK column rewritten to `passthrough`.
That column's value is the whole-table resolution of the FK key
(`_mixed_fk_bridge`), carried into the chunk stream by
`_chunked_preresolved`; it never goes through a strategy, which is why a
sibling column that reads it (`when`, `coherent_with`, a provider param) declines
rather than seeing the resolved key where the oracle shows the source key.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution import _mixed_admission as admission
from decoy_engine.execution._column_access import column_access
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.execution._chunked_input import SourceFacts
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships._graph import RelationshipEdge, RelationshipGraph

__all__ = ["MIXED_ROUTE_ENABLED", "MixedStaticPlan", "admit_mixed_static", "write_order"]

# Module-level kill switch for the whole route, beside the operator-facing one: the public
# `run_pipeline(chunked_dispatcher_enabled=False)` also turns the route off. A constant
# rather than a new `run_pipeline` kwarg keeps the frozen public signature unchanged.
MIXED_ROUTE_ENABLED: bool = True

_PASSTHROUGH_COLUMN_DEFAULTS: dict[str, Any] = {
    "strategy": "passthrough",
    "provider": None,
    "namespace": None,
    "cardinality_mode": None,
    "deterministic": False,
    "scale": None,
    "provider_config": {},
    "coherent_with": [],
    "backend_type": None,
    "backend_version": None,
    "pool_size": None,
    "vault": False,
    "allow_collisions": False,
    "dtype": None,
}


@dataclass(frozen=True)
class MixedStaticPlan:
    """A statically admitted mixed job: the optional coupled edge, the tables of
    each kind in config order, and the per-mask-table chunk configs."""

    edge: RelationshipEdge | None
    generate_tables: tuple[str, ...]
    mask_tables: tuple[str, ...]
    mask_configs: Mapping[str, dict[str, Any]]
    # Footer snapshot of each lazy mask source at admission; the route re-reads the
    # footer before publishing and declines if it moved (`facts_match`).
    source_facts: Mapping[str, SourceFacts]


def admit_mixed_static(
    config: dict[str, Any],
    *,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    table_kinds: Mapping[str, str],
    caller_sources: Mapping[str, pa.Table | LazySource],
    resolved_substrate: str,
    validators: list[Any],
    fidelity_report: bool,
    post_validation: bool,
    vault_writer: Any,
    auto_chunk: bool,
    auto_chunk_threshold_rows: int,
    chunked_dispatcher_enabled: bool,
) -> tuple[MixedStaticPlan | None, str]:
    """`(plan, "admitted")` or `(None, reason)`; the reason is a stable token for
    telemetry and tests, with a detail after the colon where one helps."""
    generate = tuple(n for n, k in table_kinds.items() if k == "generate")
    masks = tuple(n for n, k in table_kinds.items() if k == "mask")
    if not generate or not masks:
        return None, "not_mixed"
    declined = _job_setting_decline(
        resolved_substrate=resolved_substrate,
        validators=validators,
        fidelity_report=fidelity_report,
        post_validation=post_validation,
        vault_writer=vault_writer,
        quarantine=config.get("quarantine"),
        auto_chunk=auto_chunk,
        chunked_dispatcher_enabled=chunked_dispatcher_enabled,
    )
    if declined is not None:
        return None, declined
    # classify_job on a narrowed {table: source} would hide an extra caller source from its
    # unconfigured-source handling (oracle: warn-and-return or reject), and the route only
    # publishes configured tables, so decline and let the oracle decide.
    extra = sorted(n for n in caller_sources if n not in table_kinds)
    if extra:
        return None, f"unconfigured_extra_source_present:{','.join(extra)}"
    absent = [n for n in masks if n not in caller_sources]
    if absent:
        return None, f"mask_source_not_supplied:{','.join(absent)}"

    mask_set, generate_set = frozenset(masks), frozenset(generate)
    try:
        admission.require_no_out_of_core_driver(out_of_core_present=False)
        edge = admission.select_admitted_coupled_edge(
            graph, generate_set, mask_set, lambda t, c: _source_column_type(caller_sources[t], c)
        )
    except admission.MixedDeclineError as decline:
        return None, f"{decline.code}:{decline.detail}"
    if edge is None and any(
        e.parent_table in mask_set or e.child_table in mask_set for e in graph.edges
    ):
        # The core leaves a mask-side edge alone when nothing crosses; the shadow's
        # per-node loop declines it later. Here nothing runs later, so decline now.
        return (
            None,
            f"{admission.MIXED_FK_TOPOLOGY_UNSUPPORTED}:a relationship edge touches a mask table",
        )

    mask_configs: dict[str, dict[str, Any]] = {}
    for name in masks:
        per_table, reason = _mask_table_config(config, name, edge, registry)
        if per_table is None:
            return None, reason
        unstable = admission.chunk_unstable_warning_column(
            per_table["tables"][0].get("columns") or []
        )
        if unstable is not None:
            column, strategy = unstable
            return None, f"mask_strategy_warnings_not_chunk_stable:{name}.{column}:{strategy}"
        chunk_reason = _chunk_rejection(
            per_table,
            name,
            plan=plan,
            registry=registry,
            graph=graph,
            caller_sources=caller_sources,
            auto_chunk_threshold_rows=auto_chunk_threshold_rows,
        )
        if chunk_reason is not None:
            return None, f"mask_table_not_chunk_admissible:{name}:{chunk_reason}"
        mask_configs[name] = per_table
    from decoy_engine.execution import _chunked_input

    facts = _chunked_input.capture_source_facts(caller_sources, table_kinds)
    return MixedStaticPlan(edge, generate, masks, mask_configs, facts), "admitted"


def _job_setting_decline(
    *,
    resolved_substrate: str,
    validators: list[Any],
    fidelity_report: bool,
    post_validation: bool,
    vault_writer: Any,
    quarantine: Any,
    auto_chunk: bool,
    chunked_dispatcher_enabled: bool,
) -> str | None:
    if not MIXED_ROUTE_ENABLED:
        return "mixed_route_disabled"
    if not auto_chunk:
        return "auto_chunk_disabled"
    if not chunked_dispatcher_enabled:
        return "chunked_dispatcher_disabled"
    if resolved_substrate != "pandas":
        return "non_pandas_substrate_requested"
    if validators:
        return "validators_present"
    if quarantine is not None:
        return "quarantine_configured"
    if fidelity_report:
        return "fidelity_report_requested"
    if post_validation:
        return "post_validation_requested"
    if vault_writer is not None:
        return "vault_writer_requested"
    return None


def _source_column_type(source: pa.Table | LazySource, column: str) -> pa.DataType | None:
    schema = source.schema
    return schema.field(column).type if column in schema.names else None


def _mask_table_config(
    config: dict[str, Any], table: str, edge: RelationshipEdge | None, registry: Any
) -> tuple[dict[str, Any] | None, str]:
    """The job config narrowed to one mask table; `(None, reason)` when the
    coupled FK column cannot be carried safely."""
    entry = next(
        (t for t in config.get("tables") or [] if isinstance(t, dict) and t.get("name") == table),
        None,
    )
    if entry is None:
        return None, f"mask_table_config_missing:{table}"
    narrowed = copy.deepcopy(entry)
    if edge is not None and edge.child_table == table:
        key = edge.child_columns[0]
        columns = narrowed.get("columns") or []
        if not any(isinstance(c, dict) and c.get("name") == key for c in columns):
            # An unconfigured FK column is carried unchanged by the chunk lane, but the
            # oracle resolves it only when a work node exists; keep the shapes equal.
            return None, f"fk_key_column_not_configured:{table}.{key}"
        rewritten = []
        for col in columns:
            if isinstance(col, dict) and col.get("name") == key:
                rewritten.append({**col, **_PASSTHROUGH_COLUMN_DEFAULTS})
                rewritten[-1].pop("when", None)
            else:
                if not isinstance(col, dict):
                    return None, f"fk_key_column_read_unknown:{table}.{key}"
                # The declared read set, not a string scan: it parses `when:` predicates and
                # derived expressions. A read that cannot be named declines, because the
                # sibling could be reading the key.
                access = column_access(col, registry)
                if access.reads_unknown:
                    return None, f"fk_key_column_read_unknown:{table}.{key}"
                if key in access.reads:
                    return None, f"fk_key_column_read_by_sibling:{table}.{key}"
                rewritten.append(col)
        narrowed["columns"] = rewritten
    per_table = {
        **{k: v for k, v in config.items() if k not in ("tables", "relationships")},
        "tables": [narrowed],
        "relationships": [],
        "sources": {k: v for k, v in (config.get("sources") or {}).items() if k == table},
        "targets": {k: v for k, v in (config.get("targets") or {}).items() if k == table},
    }
    return per_table, "ok"


def _chunk_rejection(
    per_table_config: dict[str, Any],
    table: str,
    *,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    caller_sources: Mapping[str, pa.Table | LazySource],
    auto_chunk_threshold_rows: int,
) -> str | None:
    """Why the planner's own chunked gate refuses this table, else `None`."""
    from decoy_engine.execution import _chunked_input
    from decoy_engine.execution._planner import classify_job

    source = caller_sources[table]
    facts = {table: _chunked_input.source_facts(source)} if isinstance(source, LazySource) else {}
    decision = classify_job(
        per_table_config,
        plan=plan,
        registry=registry,
        relationship_graph=graph,
        substrate="pandas",
        source_tables={table: source},
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
        source_facts=facts,
    )
    if decision.mode == "chunked":
        return None
    return str(decision.rejections.get("chunked", decision.reason))


def write_order(
    plan: Plan, graph: RelationshipGraph, table_kinds: Mapping[str, str]
) -> tuple[str, ...]:
    """FK-topological publish order: every parent before its children, then the
    remaining tables in config order."""
    from decoy_engine.execution._sequential import table_topo_order

    topo = [t for t in table_topo_order(plan, graph) if t in table_kinds]
    return (*topo, *(t for t in table_kinds if t not in topo))
