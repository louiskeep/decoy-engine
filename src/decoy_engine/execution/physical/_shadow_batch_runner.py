"""The per-node, per-batch masking loop of `ShadowCoordinator.run`, extracted
so the coordinator stays a thin route dispatcher.

`run_shadow_batches` is what `ShadowCoordinator.run` falls through to once no
synthesis / mixed / out-of-core / full-frame branch has claimed the plan. It
reads `coordinator.ctx`, `coordinator.registry` and `coordinator.pool_cache`
and owns, byte-for-byte as before the split: the lazily loaded index kernel
(at most once per run, before the first pool build), the build-once-per-
`PoolIdentity` faker pool resolution through the job-scoped cache (never the
module-global default), per-batch `row_offset` / `rebase_row_errors`
accounting, `when_mask` slicing, the `group_key` sibling slice, the
`RESOURCE_LIMIT_BREACH` budget check, the `timed_strategy` scope (one record per
node) and source-schema-order assembly.

The coordinator is only imported for typing; the mixed branch recurses into
`ShadowCoordinator.run`, which reaches this function again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa

from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.execution._runner import in_work_order
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import (
    IndexDerivationKernel,
    load_compiled_index_kernel,
)
from decoy_engine.execution.native._operator_params import FakerParams
from decoy_engine.execution.physical._plan import ExecutionBinding, PhysicalPlan
from decoy_engine.execution.physical._shadow_assembly import (
    assemble_column,
    batch_when_mask,
    per_node_warnings,
    rebase_row_errors,
)
from decoy_engine.execution.physical._shadow_diff_codes import (
    DUPLICATE_NODE_DECLARATION,
    FAKER_POOL_NON_STRING_OUTPUT,
    NATIVE_COMPANION_UNAVAILABLE,
    OPERATOR_NOT_EXECUTED,
    PLANNED_VS_ACTUAL_ROUTE_DIFF,
    RESOURCE_LIMIT_BREACH,
    SCHEMA_DIFF,
    PoolBuildFailed,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_fk import build_fk_dispatch, resolve_admitted_fk_node
from decoy_engine.execution.physical._shadow_operators import (
    OperatorCallEvidence,
    operator_invariants_fail_loud,
    run_operator,
)
from decoy_engine.execution.physical._shadow_result import ShadowRunResult
from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
from decoy_engine.generation.pool import PoolBuilder, PoolCache, ValuePool
from decoy_engine.generation.pool._identity import PoolIdentity, resolve_faker_pool_identity
from decoy_engine.instrumentation.timing import timed_strategy

if TYPE_CHECKING:
    from collections.abc import Mapping

    from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.relationships._graph import RelationshipEdge

__all__ = ["run_shadow_batches"]


def _pool_values_are_string_valued(values: np.ndarray[Any, Any]) -> bool:
    """True iff every value `values` holds is a string.

    `sample_faker_array` (`native/_chunk_masking.py`) always casts the
    gathered selection to `pa.string()`; a pool built from a custom-registry
    adapter that yields non-string values (an int, a float) would otherwise
    reach that cast and raise an uncoded `ArrowTypeError` instead of a coded
    shadow difference. A native numpy string dtype (`U`/`S`) is always fine;
    an object-dtype array (how `PoolBuilder.build` always stores pool
    values) is fine only when every non-null element is actually a `str` --
    a numeric dtype, or an object array holding a non-str/non-None element,
    fails the check.
    """
    kind = values.dtype.kind
    if kind in ("U", "S"):
        return True
    if kind != "O":
        return False
    return all(v is None or isinstance(v, str) for v in values.tolist())


def _batches(table: pa.Table, batch_size_rows: int) -> list[pa.Table]:
    """Order-preserving, row-identity-preserving batching. A zero-row table
    yields exactly ONE zero-row batch (C6): ordinary slicing produces no
    batch at all for an empty table, which would leave every planned
    operator unexecuted and the hash node's compiled-kernel evidence
    vacuous.
    """
    if table.num_rows == 0:
        return [table]
    return [
        table.slice(offset, batch_size_rows) for offset in range(0, table.num_rows, batch_size_rows)
    ]


def run_shadow_batches(
    coordinator: ShadowCoordinator,
    plan: PhysicalPlan,
    snapshot: ShadowSnapshot,
    *,
    admitted_edges: tuple[RelationshipEdge, ...] = (),
    when_masks: Mapping[str, pa.Array] | None = None,
) -> ShadowRunResult:
    outputs: dict[str, pa.Table] = {}
    route_evidence: dict[str, OperatorCallEvidence] = {}
    # Loaded at most once per run, lazily, the moment the first bound
    # faker node is reached -- and before that node's pool is built.
    index_kernel: IndexDerivationKernel | None = None
    # The authoritative build-once-per-identity store for this run,
    # keyed by `PoolIdentity` (NOT node_id): two nodes sharing an
    # identity share one build, and an A->B->A node order cannot rebuild
    # A. Backed by the injected job-scoped cache, else a fresh run-scoped
    # `PoolCache`, never the module-global default (see the module docstring).
    pools_by_identity: dict[PoolIdentity, ValuePool] = {}
    pool_cache = coordinator.pool_cache if coordinator.pool_cache is not None else PoolCache()
    # Slice 5b-ii: an FK-child column resolves against its parent ahead of native binding.
    fk = build_fk_dispatch(snapshot, coordinator.ctx.relationship_graph, admitted_edges)
    warnings: list[QualityWarning] = []
    row_errors: list[RowErrorRecord] = []

    for table in plan.tables:
        source = snapshot.tables[table.table]
        columns: dict[str, pa.Array] = {}
        for node in in_work_order(table.table, table.nodes):
            fk_resolution = resolve_admitted_fk_node(node, table.table, source, columns, fk)
            if fk_resolution is not None:
                columns[node.columns[0]] = fk_resolution.column
                warnings.extend(fk_resolution.warnings)
                continue
            binding = node.execution
            if binding is None:
                # Out of the slice (a different strategy, or a native-admission miss).
                continue
            column = node.columns[0]
            if node.node_id in route_evidence:
                # A duplicate column+strategy declaration (config accepts it)
                # collides on node_id and would collapse two nodes into one
                # evidence record + one output column. Surface it, don't hide it.
                raise ShadowDifference(
                    code=DUPLICATE_NODE_DECLARATION,
                    detail=f"{table.table}: duplicate node_id {node.node_id!r}",
                )
            evidence = OperatorCallEvidence(planned_operator=binding.operator_id)
            route_evidence[node.node_id] = evidence

            with timed_strategy(node.strategy, ",".join(node.columns)):
                pool: ValuePool | None = None
                if binding.needs_index_kernel and index_kernel is None:
                    try:
                        index_kernel = load_compiled_index_kernel()
                    except CryptoExtensionUnavailableError as exc:
                        raise ShadowDifference(
                            code=NATIVE_COMPANION_UNAVAILABLE,
                            detail=(f"node={node.node_id!r}: compiled index companion unavailable"),
                        ) from exc
                if binding.pool_binding is not None:
                    pool = _resolve_pool(
                        coordinator,
                        binding=binding,
                        pools_by_identity=pools_by_identity,
                        pool_cache=pool_cache,
                    )

                # group_key alone reads a DIFFERENT column than it writes: it keys
                # on the sibling `group_by`'s original source value (admission
                # proved the sibling unmasked, so it equals what the oracle reads)
                # and writes to `column`; every other operator reads `column`.
                input_column = binding.group_key_sibling or column
                parts: list[pa.Array] = []
                # `_batches` slices in order from 0, so the running row count is
                # each batch's table-global start offset.
                row_offset = 0
                for batch in _batches(source, coordinator.ctx.batch_size_rows):
                    if batch.num_rows > coordinator.ctx.batch_size_rows:  # pragma: no cover
                        raise ShadowDifference(
                            code=RESOURCE_LIMIT_BREACH,
                            detail=f"node={node.node_id!r}: a batch exceeded the batch_size_rows budget",
                        )
                    array = batch.column(input_column)
                    # group_key stringifies its sibling exactly as the oracle's
                    # frame does, which turns on the source's `b"pandas"` sidecar
                    # (StringDtype null -> "<NA>", not "None"). A bare column drops
                    # that sidecar, so feed the single-column source SLICE, which
                    # preserves the field name and schema metadata `table.slice`
                    # carried through `_batches`.
                    group_key_sibling = (
                        batch.select([input_column])
                        if binding.group_key_sibling is not None
                        else None
                    )
                    with operator_invariants_fail_loud(binding.operator_id):
                        out, batch_errors = run_operator(
                            array,
                            binding=binding,
                            ctx=coordinator.ctx,
                            evidence=evidence,
                            pool=pool,
                            index_kernel=index_kernel,
                            group_key_sibling=group_key_sibling,
                            column=column,
                            row_offset=row_offset,
                            when_mask=batch_when_mask(
                                when_masks, node.node_id, row_offset, batch.num_rows
                            ),
                            source_slice=batch.select([column]),
                        )
                    parts.append(out)
                    row_errors.extend(rebase_row_errors(table.table, batch_errors, row_offset))
                    row_offset += batch.num_rows

                if not evidence.executed:  # pragma: no cover - run_operator always sets this
                    raise ShadowDifference(
                        code=OPERATOR_NOT_EXECUTED,
                        detail=f"node={node.node_id!r}: the bound operator never ran",
                    )
                if evidence.actual_operator != binding.operator_id:
                    raise ShadowDifference(
                        code=PLANNED_VS_ACTUAL_ROUTE_DIFF,
                        detail=(
                            f"node={node.node_id!r}: planned={binding.operator_id!r} "
                            f"actual={evidence.actual_operator!r}"
                        ),
                    )

                columns[column] = assemble_column(node.strategy, parts)
                warnings.extend(per_node_warnings(binding, source, column, evidence))

        if columns:
            # Assemble in SOURCE-SCHEMA order -- the pandas full-frame
            # oracle preserves the source column order, NOT the work order
            # this loop iterates in (Codex final-gate
            # HIGH: source [a,b] with config [b,a] otherwise diverged). For
            # the bounded slice EVERY source column must be configured with
            # an in-slice strategy, so the assembled set must equal the
            # source set exactly; a missing (unconfigured source column) or
            # unexpected column is a coded difference, not a silent drop.
            source_order = source.column_names
            if set(columns) != set(source_order):
                raise ShadowDifference(
                    code=SCHEMA_DIFF,
                    detail=(
                        f"{table.table}: assembled columns {sorted(columns)} "
                        f"!= source columns {sorted(source_order)}"
                    ),
                )
            outputs[table.table] = pa.table({name: columns[name] for name in source_order})

    return ShadowRunResult(
        outputs=outputs,
        route_evidence=route_evidence,
        warnings=tuple(warnings),
        row_errors=tuple(row_errors),
    )


def _resolve_pool(
    coordinator: ShadowCoordinator,
    *,
    binding: ExecutionBinding,
    pools_by_identity: dict[PoolIdentity, ValuePool],
    pool_cache: PoolCache,
) -> ValuePool:
    """Resolve one faker node's pool, ONCE, outside the batch loop: the
    run-scoped identity map first (the build-once store; an LRU cache alone
    could evict between two nodes sharing an identity), then the `PoolCache`,
    and `PoolBuilder` only on a miss on both. Uses the same
    `resolve_faker_pool_identity` as the oracle and the native chunked route.
    """
    params = binding.params
    if binding.pool_binding is None or not isinstance(params, FakerParams):  # pragma: no cover
        raise AssertionError("_resolve_pool called without a pool_binding/FakerParams")
    if coordinator.registry is None:
        raise AssertionError(
            "a faker node is bound but ShadowCoordinator.registry is None; the "
            "shadow caller must thread the resolved ProviderRegistry (inputs.registry) "
            "for any run admitting a faker node."
        )
    if coordinator.ctx.job_seed == b"":
        # Symmetric to the registry guard: an empty job_seed would build a
        # wrong-but-passing pool; from_key_provider always supplies the real one.
        raise AssertionError(
            "a faker node is bound but ShadowContext.job_seed is empty; the shadow "
            "caller must build the context via from_key_provider so job_seed is set."
        )
    builder = PoolBuilder(coordinator.registry)
    pool_size, locale, build_config, identity = resolve_faker_pool_identity(
        builder=builder,
        provider=binding.pool_binding.provider,
        plan_pool_size=binding.pool_binding.plan_pool_size,
        namespace=params.namespace,
        job_seed=coordinator.ctx.job_seed,
        cfg=dict(binding.resolved_config),
    )
    pool: ValuePool
    cached_pool = pools_by_identity.get(identity)
    if cached_pool is not None:
        pool = cached_pool
        # Touch the LRU like the oracle's reuse does; a pinned hit would skip it.
        pool_cache.get(identity)
    else:
        from_secondary_cache = pool_cache.get(identity)
        built = from_secondary_cache if isinstance(from_secondary_cache, ValuePool) else None
        if built is None:
            try:
                built = builder.build(
                    provider=binding.pool_binding.provider,
                    size=pool_size,
                    job_seed=coordinator.ctx.job_seed,
                    locale=locale,
                    config=build_config,
                    namespace=params.namespace,
                )
                pool_cache.put(built)
            except Exception as exc:
                raise PoolBuildFailed(exc) from exc
        pool = built
        pools_by_identity[identity] = pool

    # Admission (`_faker_pool_bindable`) proves only the provider NAME is allowlisted and
    # poolable. A custom-registry override can rebind that name to a poolable adapter
    # yielding non-string values, so the pool's real value type is checked here, at the
    # one point it is known, rather than trusting admission's weaker guarantee.
    if not _pool_values_are_string_valued(pool.values):
        raise ShadowDifference(
            code=FAKER_POOL_NON_STRING_OUTPUT,
            detail=(
                f"provider={binding.pool_binding.provider!r}: pool values are not "
                "all string-valued, which the shadow faker operator requires"
            ),
        )
    return pool
