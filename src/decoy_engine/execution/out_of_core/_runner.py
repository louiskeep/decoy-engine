"""Batch-streaming runner for the out-of-core FK route.

Executes a relational FK job as bounded batch passes per table, in
FK-topological order, so no whole table is ever resident on the lazy-source
path:

1. Rewrite pass: the raw source streams through `mask_batch` (non-FK
   columns) and one `ChildFkBatchJoiner` per incoming edge (FK columns).
   Every joiner keys off the IMMUTABLE raw batch while its rewrite
   accumulates in the output batch: DISTINCT edges may overlap on a child
   column, and the whole-child contract is that each edge joins the RAW child,
   with later edges overwriting the shared column. Edges sharing one child
   tuple (a multi-parent child) join ONE relation merged first-hit-wins in
   edge order (`_multi_parent.resolve_incoming`), through the first edge.
   The rewritten batches flow straight
   into `TransactionalSink.write_batches` under one analytically fixed
   schema, because an Arrow ParquetWriter writes a single-schema file (the
   same constraint pyarrow.dataset resolves by unifying fragment schemas up
   front). Tables with outgoing edges tee their key columns to a narrow
   staged Parquet copy on the way through (`_stage.py`).
2. Relation build: every outgoing parent-key relation maps the RAW key (the
   join key a child still holds) to the table's FINAL post-rewrite value,
   taken from the staged copy (sink path) or the reassembled resident table
   (no-sink path), never from a re-mask of the raw stream: an incoming edge
   may have rewritten the key column to a value the column's own plan seed
   cannot reproduce, and the published table is the only truth children may
   reference.

Re-reading a source once per pass trades bounded RAM for disk IO, the
standard external-memory discipline (DuckDB's larger-than-memory hash join
re-reads its spilled partitions the same way). A resident `pa.Table` source
re-iterates without extra disk IO, but holds the WHOLE input resident -- free
for CPU, not for memory; the residency bound (bounded peak residency w.r.t. row
cardinality) holds only for `LazySource` sources plus an incrementally-consuming
sink (others get a residency `QualityWarning`). Without a sink, the streamed batches
are reassembled in memory column-wise under the whole-child chunk-merge rules
(`_join.py`), so the returned tables keep the value-derived column types the
pandas-oracle parity suite pins.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.declared_casting import _stream as _declared
from decoy_engine.declared_casting._boundary import seal_evidence
from decoy_engine.declared_casting._eligibility import declared_route_guard
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._output_projection import (
    UnconfiguredColumnPolicy,
)
from decoy_engine.execution._publication_observer import observed_abort, observed_commit
from decoy_engine.execution._transactional_sink import TransactionalSink
from decoy_engine.execution.out_of_core import _join as _join_ooc
from decoy_engine.execution.out_of_core import _validate_batch
from decoy_engine.execution.out_of_core._budget import check_temp_disk_budget
from decoy_engine.execution.out_of_core._faker_pool import OocFakerPoolContext
from decoy_engine.execution.out_of_core._hash_run import OocHashRun
from decoy_engine.execution.out_of_core._mask import mask_batch  # noqa: F401 -- patch seam
from decoy_engine.execution.out_of_core._memory_estimate import (
    memory_limit_for,
)
from decoy_engine.execution.out_of_core._multi_parent import resolve_incoming
from decoy_engine.execution.out_of_core._preflight import run_preflight
from decoy_engine.execution.out_of_core._relation import _column_seed  # noqa: F401 -- re-exported
from decoy_engine.execution.out_of_core._rewrite_pass import (  # noqa: F401 -- patch seams
    _iter_source_batches,
    _stream_table,
)
from decoy_engine.execution.out_of_core._route_policy import (
    _edge_indexes,
    _table_order,
    decide_route,
    validate_outgoing_parent_columns,
)
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_driver import stream_table
from decoy_engine.progress import get_active_channel

if TYPE_CHECKING:
    from typing import TypeAlias

    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph
    from decoy_engine.relationships._graph import RelationshipEdge

    # One source per table: a resident Arrow table (back-compat) or a
    # path-backed lazy reader (the bounded-residency capability path).
    TableSource: TypeAlias = pa.Table | LazySource


def run_fk_out_of_core(
    plan: Plan,
    sources: Mapping[str, pa.Table | LazySource],
    *,
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
    sink: TransactionalSink | None = None,
    temp_dir: Path | None = None,
    memory_limit: str | None = None,
    batch_rows: int | None = None,
    budget_bytes: int | None = None,
    temp_disk_budget_bytes: int | None = None,
    unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
    key_provider: KeyProvider | None = None,
    out_of_core_reorder_threshold_rows: int | None = None,
    vault_writer: Any = None,
    config: Mapping[str, Any] | None = None,
    native_threads: int = 1,
    cast_run: Any = None,
    _staged: bool = False,
) -> ExecutionResult:
    """Run the out-of-core FK relationship route as a batch stream.

    Private entrypoint. Handles single- and composite (multi-column) FK edges,
    chains and fanouts across the relationship graph, and any of the admitted
    parent key strategies (hash, redact, truncate, passthrough); see
    `check_out_of_core_compatibility` for the exact admitted surface, which is
    what keeps this byte-identical to the pandas oracle for accepted plans
    EXCEPT the divergences inventoried in `_batch_join.py`'s module docstring
    (fixed-schema typing and composite partial-null orphan handling).
    With a sink, each table is staged as a stream of bounded record batches
    (never whole-table resident); without one, the batches are reassembled into
    in-memory tables with whole-column type semantics. Inputs must already be transformed.

    Residency precondition (caller-managed; P4-A). This exported primitive does
    not enforce boundedness: its peak residency is bounded w.r.t. row cardinality
    only when every `sources` value is a `LazySource` and `sink` consumes
    `write_batches` incrementally without retaining the stream
    (`ParquetTransactionalSink`). A resident source or `sink=None` holds a whole
    table in RAM; a direct caller of this primitive owns that memory risk (the
    managed `run_pipeline` route warns for those shapes, this primitive does not).

    `batch_rows` bounds every streamed pass (default: the pinned module
    constant); any legal value is byte-transparent on the output.
    `budget_bytes` (the UNDIVIDED per-run budget) is preferred over the flat
    `memory_limit` string when given: each DuckDB connection this route opens
    is then capped by its own PHASE-local liveness via `_memory_estimate.
    resolve_phase_memory_limits`, not the run's single global-peak divisor
    (SPRINT-1 Part A; see that module's docstring for the root cause this
    fixes). `memory_limit` alone is the fallback -- every connection gets the
    same flat cap, matching pre-Part-A behavior -- used when `budget_bytes`
    is None (host-RAM detection failed and no explicit budget was supplied).
    `temp_disk_budget_bytes` bounds the spill footprint under this run's
    temp root, checked at each table boundary; exceeding it aborts the sink
    and fails closed.

    `out_of_core_reorder_threshold_rows` (P4-A Task 7): the sink-path
    per-table route seam auto-selects `_stream_driver.stream_table` (the
    reorder driver) over this module's own `_stream_table` (`_batch_join`)
    once a table's largest incoming edge's deduped parent-key count reaches
    this threshold; see `_route_policy.decide_route` for the full predicate
    and `resolve_reorder_threshold_rows` for the override's `None`/`0`/
    negative/`bool` semantics. Route choice changes timing and memory, never
    output (`_route_policy.py`'s module docstring).

    `vault_writer` + `config` (D3b): both drivers add each masked batch's `vault: true`
    pairs to the writer at their mask step. Routing admits only jobs whose vaulted columns
    are route-stable (`_vault_collect.vault_ooc_safe`); persisting is the caller's, after a
    successful return.
    """
    declared_route_guard("out_of_core", plan=plan, raise_on_decline=True)
    if cast_run is not None and not _staged and cast_run.tables() & set(sources):
        # F06: declared sources are cast once into typed sources (see `run_staged`), then this
        # function runs again over them.
        return _declared.run_staged(run_fk_out_of_core, dict(locals()), _join_ooc._JOIN_BATCH_ROWS)
    pre = run_preflight(
        plan,
        sources,
        registry=registry,
        relationship_graph=relationship_graph,
        key_provider=key_provider,
        vault_writer=vault_writer,
        config=config,
        batch_rows=batch_rows,
        out_of_core_reorder_threshold_rows=out_of_core_reorder_threshold_rows,
    )
    mask_key, reorder_threshold_rows = pre.mask_key, pre.reorder_threshold_rows

    validators = _validate_batch.build_validator_collector(
        config, plan, relationship_graph, sources
    )
    owned_temp = temp_dir is None
    root = Path(tempfile.mkdtemp(prefix="decoy-ooc-")) if temp_dir is None else temp_dir
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    committed = False
    # B4: the run-scoped faker pool lifecycle. Pools are Python/object memory
    # outside DuckDB's accounting, so bound them well inside the run budget and
    # never past the pool cache's own 256 MiB default. Constructed once per run
    # (cheap: a cache plus a registry-bound builder, no kernel load); OocHashRun
    # only consults it when a table carries an admitted faker column.
    _faker_pool_cap = 256 * 1024 * 1024
    faker_pool = OocFakerPoolContext(
        registry=registry,
        job_seed=plan.seed_envelope.job_seed,
        max_pool_bytes=min(budget_bytes, _faker_pool_cap) if budget_bytes else _faker_pool_cap,
    )
    try:
        incoming, outgoing = _edge_indexes(relationship_graph)
        parent_relations: dict[RelationshipEdge, ParentKeyRelation] = {}
        outputs: dict[str, pa.Table] = {}
        warnings: list[QualityWarning] = []
        # HIGH-2 remediation (HC-1 slice 1 gap): the code_set corpus-provenance
        # evidence sink, mirroring `StrategyContext.code_set_corpora` on the
        # pandas/sequential routes. Keyed by (table, column) -- Codex P2
        # MULTI-TABLE EVIDENCE COLLISION remediation: two tables can legally
        # declare a same-named code_set column bound to different corpora, and
        # a bare-column key let the second table's stamp silently overwrite
        # the first's. Same shape as the full-frame sink, so a multi-table
        # job's evidence list matches the pandas oracle's shape byte-for-byte.
        code_set_corpora: dict[tuple[str, str], dict[str, Any]] = {}
        hash_run = OocHashRun(native_threads=native_threads, faker_pool=faker_pool)
        for table_name in _table_order(plan, relationship_graph, sources):
            if table_name not in sources:
                continue
            # The merge runs before any joiner opens, so it takes the undivided build cap.
            table_incoming_edges = resolve_incoming(
                incoming[table_name],
                parent_relations,
                temp_dir=root / "relations" / table_name / "merged",
                memory_limit=memory_limit_for(budget_bytes, 1) if budget_bytes else memory_limit,
            )
            table_outgoing_edges = tuple(outgoing[table_name])
            _pg = get_active_channel()
            _pg_started = _pg.phase_start("mask", table=table_name)
            # Route-independent gate: both drivers raise the same coded error (not a bare KeyError).
            validate_outgoing_parent_columns(table_outgoing_edges, sources[table_name].schema)
            route = decide_route(
                table_incoming_edges,
                parent_relations,
                sink=sink,
                budget_bytes=budget_bytes,
                temp_disk_budget_bytes=temp_disk_budget_bytes,
                threshold_rows=reorder_threshold_rows,
            )
            # Shared by both drivers; the reorder branch adds only memory-sizing kwargs.
            table_kwargs: dict[str, Any] = {
                "plan": plan,
                "table_name": table_name,
                "raw": sources[table_name],
                "incoming_edges": table_incoming_edges,
                "outgoing_edges": table_outgoing_edges,
                "parent_relations": parent_relations,
                "temp_dir": root / "joins" / table_name,
                "relation_dir": root / "relations" / table_name,
                "staging_path": root / "staged" / table_name / "masked_keys.parquet",
                "memory_limit": memory_limit,
                "batch_rows": batch_rows,
                "budget_bytes": budget_bytes,
                "sink": sink,
                "outputs": outputs,
                "warnings": warnings,
                "unconfigured_column_policy": unconfigured_column_policy,
                "mask_key": mask_key,
                "code_set_corpora": code_set_corpora,
                "hash_run": hash_run,
                "vault_writer": vault_writer,
                "vault_config": config,
                "validators": validators,
                "cast_run": cast_run,
            }
            # `reorder_caps is not None` (not `route.use_reorder`) is the branch
            # condition so this narrows for mypy directly, matching `RouteDecision`'s
            # own invariant that the two fields never disagree.
            if route.reorder_caps is not None:
                stream_table(
                    **table_kwargs,
                    run_bytes_cap=route.reorder_caps.run_bytes_cap,
                    merge_fan_in=route.reorder_caps.merge_fan_in,
                    reorder_caps=route.reorder_caps,
                )
            else:
                _stream_table(**table_kwargs)
            _pg.flush(table_name)
            _pg.phase_finish("mask", _pg_started, table=table_name)
            if temp_disk_budget_bytes is not None:
                # Table boundaries are the natural checkpoints: the spill
                # footprint peaks with each table's relation/join staging, and
                # a walk here costs a handful of stats, not a watcher thread.
                check_temp_disk_budget(root, max_bytes=temp_disk_budget_bytes)
        quality_metrics: dict[str, Any] = {
            **({"code_set_corpora": list(code_set_corpora.values())} if code_set_corpora else {}),
            **hash_run.metrics(),
        }
        # Before the publish: a failed validation commits nothing.
        _validate_batch.finish_validation(validators, quality_metrics)
        seal = {"quarantine_access": seal_evidence(cast_run, quality_metrics)}  # last, pre-publish
        if sink is not None:
            observed_commit(sink)
            committed = True
            return ExecutionResult(
                outputs={}, warnings=tuple(warnings), quality_metrics=quality_metrics, **seal
            )
        return ExecutionResult(
            outputs=outputs, warnings=tuple(warnings), quality_metrics=quality_metrics, **seal
        )
    except Exception as failure:
        if sink is not None and not committed:
            observed_abort(sink, failure)
        raise
    finally:
        # The relation/join staging subtrees hold (source_key, masked_key) pairs
        # on disk and the staged subtree holds output values (which can include
        # preserved raw orphan keys); a caller-supplied temp_dir is NOT torn
        # down (owned_temp below is False), so without this those keys sit at
        # rest after a successful run. Only the parts the runner itself created
        # get wiped here; the rest of a caller-owned root is the caller's
        # responsibility.
        shutil.rmtree(root / "relations", ignore_errors=True)
        shutil.rmtree(root / "joins", ignore_errors=True)
        shutil.rmtree(root / "staged", ignore_errors=True)
        if owned_temp:
            shutil.rmtree(root, ignore_errors=True)
        # Release the run's faker pools (in-memory numpy arrays) and reset the
        # reservation ledger; nothing survives the run.
        faker_pool.close()


__all__ = ["run_fk_out_of_core"]
