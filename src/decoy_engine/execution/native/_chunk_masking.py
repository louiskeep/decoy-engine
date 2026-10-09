"""Per-chunk native masking (compiled kernels + faker pool selection).

Split out of `_dispatch.py` (module-size ratchet, native-throughput program):
`_mask_chunk_native` is the chunked route's adapter around the shared kernel step
(`_operator_step.run_kernel_step`) once a table has already been admitted to the native
route, while `_dispatch` owns the PREFLIGHT route decision (admission, evidence, the
oracle/native fork). The dependency is one-directional -- `_dispatch` imports these functions
back -- so this module must never import `_dispatch` at runtime (that would be a cycle); the
`NativeRouteEvidence` type whose counters it mutates is imported only under `TYPE_CHECKING`
for annotations, and mutated here via plain attribute access.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.native._operator_params import (
    BucketPerturbParams,
    CategoricalParams,
    FakerParams,
    GroupKeyParams,
    OperatorParams,
    is_positional_faker_seed,
)
from decoy_engine.execution.native._operator_step import run_kernel_step, run_kernel_step_masked
from decoy_engine.generation.pool import PoolBuilder, PoolCache, ValuePool
from decoy_engine.generation.pool._identity import resolve_faker_pool_identity
from decoy_engine.providers_v2 import get_default_registry

if TYPE_CHECKING:
    from decoy_engine.execution.native._dispatch import NativeRouteEvidence
    from decoy_engine.execution.native._group_key_ext import RawHexDerivationKernel
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel


NONSTRING_POOL_CODE = "chunked_faker_nondeterministic_pool_not_string"


def _mask_chunk_native(
    chunk: pa.Table,
    *,
    col_seed_by_name: dict[str, Any],
    mask_key: bytes | None,
    evidence: NativeRouteEvidence,
    pool_by_column: dict[str, ValuePool],
    native_threads: int | None = None,
    index_kernel: IndexDerivationKernel | None = None,
    column_elapsed_s: dict[str, float] | None = None,
    unconfigured: frozenset[str] = frozenset(),
    stored_index: frozenset[str] = frozenset(),
    params_by_column: dict[str, OperatorParams] | None = None,
    kernel_idle: set[str] | None = None,
    row_offset: int = 0,
    format_errors: dict[str, tuple[int, ...]] | None = None,
    raw_chunk: pa.Table | None = None,
    raw_hex_kernel: RawHexDerivationKernel | None = None,
    job_seed: bytes | None = None,
    when_masks: Mapping[str, pa.Array] | None = None,
    faker_missing: Mapping[str, pa.Array] | None = None,
) -> pa.Table:
    """Mask one chunk column-by-column through the admitted native kernels.

    A column named in `unconfigured` has no plan node: it is passed through as the source
    column, with no kernel, no timing and no seed lookup (the caller applies the
    unconfigured-column policy and its warning).

    A column named in `stored_index` (a pandas index field the oracle route consumes as the
    index) is left out of the result, as the oracle route leaves it out.

    `column_elapsed_s`, when given, receives each column's kernel time for this
    chunk, from the same timer that feeds the per-strategy aggregate (one clock
    read pair per column, nothing sampled beyond it).

    Every column name in `chunk` is guaranteed present in `col_seed_by_name` or
    `unconfigured` by the caller's admission precondition (preflight rejects a table
    with an uncovered column unless it is admitted as unconfigured passthrough), so a
    missing lookup here is a precondition violation, not a data-shape surprise.
    `pool_by_column` is populated once, before the chunk loop, for every admitted faker
    column (Task 3.1 Step 2); a faker column always has an entry by the same
    precondition. `index_kernel` is the preflight-verified compiled index
    kernel (Task 2.3): non-`None` whenever the admitted table has a faker,
    categorical, bucket_perturb or date_shift column, since preflight's index probe already ran
    before this ever executes. `params_by_column` holds each admitted column's resolved operator
    parameters (categorical categories and CDF included), built once per table and reused by
    every chunk.

    `kernel_idle`, when given, receives each column that ran its strategy branch but no
    compiled kernel this chunk (a bucket_perturb or date_shift chunk with no parseable row, an
    empty group_key chunk, or a zero-row chunk of the seeded categorical), so the chunk's route
    evidence does not credit the companion for work it did not do.

    `format_errors`, when given, receives each date_shift column's chunk-local positions of
    non-null values that did not parse. The oracle chunked leg records `row_index` within its
    chunk and never adds the chunk's global offset, so these are not rebased either. A date_shift
    column that has such positions with no `format_errors` to carry them raises: dropping them
    would let the raw value reach the output with the job succeeding.

    `raw_chunk` is the chunk as the source produced it, before null-typed columns were cast to
    the first chunk's types; only a group_key column reads it (for its sibling), every other
    branch masks `chunk`. It defaults to `chunk`, which is the same table when nothing was cast.
    The oracle stringifies the raw chunk (a null-typed later chunk's null is "None" there, "<NA>"
    once cast to the first chunk's integer type), and `cast_null_columns` drops the schema
    metadata that decides a nullable string or boolean sibling's dtype, so the cast table would
    key a null differently. `raw_hex_kernel` is the preflight-verified raw-hex kernel group_key
    derives with.

    `when_masks` holds the row mask of each admitted `when:` column for this chunk (see
    `_when_mask`); such a column goes through `run_kernel_step_masked`, and a chunk where the
    predicate selects no row is idle and uncounted, like an empty positional chunk.

    `faker_missing` holds each positional Faker column's missing mask for this chunk, taken from
    the oracle's conversion of the raw chunk (see `_faker_null_mask`).

    `row_offset` is the global position of the chunk's first row; only the seeded
    non-deterministic categorical and the position-keyed faker key on it (the faker also on
    `job_seed`). A zero-row chunk of either makes no compiled call (idle, uncounted, typed empty
    `string`); an all-null non-empty one does run it.
    """
    arrays: dict[str, pa.Array] = {}
    for name in chunk.schema.names:
        if name in stored_index:
            continue
        if name in unconfigured:
            arrays[name] = chunk.column(name)
            continue
        strategy = col_seed_by_name[name].strategy
        params = (params_by_column or {}).get(name)
        if params is None:  # pragma: no cover - admission implies parameters for every column
            raise AssertionError(
                f"native route admitted column {name!r} with strategy {strategy!r} but no "
                "resolved operator parameters; the preflight admission check should have "
                "excluded this table, or `_prepared_categoricals` should have prepared it."
            )
        source = chunk.column(name)
        t0 = time.perf_counter()
        sibling: pa.Table | None = None
        if isinstance(params, GroupKeyParams):
            sibling = (chunk if raw_chunk is None else raw_chunk).select([params.group_by])
            if raw_hex_kernel is None:  # pragma: no cover - admission implies a loaded kernel
                raise AssertionError(
                    "native route admitted a group_key column with no raw_hex_kernel; "
                    "preflight's raw-hex probe should have loaded one for any admitted node."
                )
        when_mask = (when_masks or {}).get(name)
        if when_mask is not None:
            result = run_kernel_step_masked(
                params,
                source,
                when_mask,
                mask_key=mask_key,
                native_threads=native_threads,
                index_kernel=index_kernel,
                pool=pool_by_column[name] if isinstance(params, FakerParams) else None,
                row_offset=row_offset,
                job_seed=job_seed,
                missing_mask=(faker_missing or {}).get(name),
            )
        else:
            result = run_kernel_step(
                params,
                source,
                mask_key=mask_key,
                native_threads=native_threads,
                index_kernel=index_kernel,
                raw_hex_kernel=raw_hex_kernel,
                pool=pool_by_column[name] if isinstance(params, FakerParams) else None,
                sibling=sibling,
                row_offset=row_offset,
                job_seed=job_seed,
                missing_mask=(faker_missing or {}).get(name),
            )
        out = result.out
        # The kernel always returns `pa.string()`, but the oracle chunked route gives Arrow `null`
        # for a zero-row or all-null bucket_perturb chunk (promotable when the chunks are joined)
        # and `string` for any chunk holding a value, an all-unparseable one included.
        if isinstance(params, BucketPerturbParams) and (
            len(out) == 0 or out.null_count == len(out)
        ):
            out = pa.nulls(len(out))
        arrays[name] = out
        counted = True
        if isinstance(params, FakerParams) and result.ran:
            evidence.pool_select_executed = True
            evidence.pool_select_calls += 1
        elif result.ran:
            evidence.compiled_kernel_executed = True
        elif result.ran is False:
            counted = when_mask is None and not isinstance(params, (CategoricalParams, FakerParams))
            if kernel_idle is not None:
                kernel_idle.add(name)
        if result.format_error_positions:
            if format_errors is None:
                raise AssertionError(
                    f"date_shift column {name!r} has unparseable values but the caller "
                    "gave no format_errors channel to carry them."
                )
            format_errors[name] = result.format_error_positions
        elapsed = time.perf_counter() - t0
        if counted:
            evidence.kernel_calls[strategy] = evidence.kernel_calls.get(strategy, 0) + 1
        evidence.kernel_elapsed_s[strategy] = evidence.kernel_elapsed_s.get(strategy, 0.0) + elapsed
        if column_elapsed_s is not None:
            column_elapsed_s[name] = elapsed
    return pa.table(arrays)


def pool_values_are_strings(pool: ValuePool) -> bool:
    """True when every non-null pool value is a string, the one output type the
    native sampler's string gather reproduces. The pool is fixed once built, so
    this runs once per pool at admission, never per chunk."""
    try:
        values = pa.array(pool.values)
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError):
        return False
    return (
        pa.types.is_string(values.type)
        or pa.types.is_large_string(values.type)
        or pa.types.is_null(values.type)
    )


def reject_nonstring_positional_pools(state: Any, *, table: str) -> None:
    """Fail closed when a position-keyed faker column's pool holds non-string values.

    Runs eagerly on BOTH chunked legs, before any masking, normalization or write, whether or
    not the table is native-admitted: the column's output type is pinned to `string` on every
    chunk, so a custom provider registered under an allowlisted name that returns another type
    would silently change the column's type relative to the whole-frame run. The pools are
    cached in `state.pool_cache`, so the leg that runs reuses them.

    Raises:
        ExecutionError: ``code='chunked_faker_nondeterministic_pool_not_string'``.
    """
    envelope = state.plan.seed_envelope
    table_seed = next((ts for (name, ts) in envelope.per_table if name == table), None)
    if table_seed is None:  # pragma: no cover - a validated mask table always has a seed envelope
        return
    positional = {n: s for n, s in table_seed.per_column if is_positional_faker_seed(s)}
    pools = _resolve_faker_pools(
        positional,
        job_seed=envelope.job_seed,
        pool_cache=state.pool_cache,
        registry=state.registry,
    )
    for column, pool in pools.items():
        if not pool_values_are_strings(pool):
            raise ExecutionError(
                code=NONSTRING_POOL_CODE,
                message=(
                    f"column {column!r}: provider {positional[column].provider!r} returns "
                    "non-string values, but a non-deterministic faker column runs chunked with "
                    "a string output type on every chunk, which would change this column's type "
                    "relative to the whole-frame run. Disable auto-chunking for this job or "
                    "register the provider under a different name."
                ),
            )


def _resolve_faker_pools(
    col_seed_by_name: dict[str, Any],
    *,
    job_seed: bytes,
    pool_cache: PoolCache,
    registry: Any = None,
) -> dict[str, ValuePool]:
    """Build/fetch every admitted faker column's pool ONCE, before the chunk
    loop (Task 3.1 Step 2). Keyed by unique `PoolIdentity`, not by column: two
    columns sharing provider + locale + config + namespace share one pool and
    one `pool_cache` entry, matching `FakerStrategyHandler`'s own per-chunk
    cache consult on the oracle side. Uses the SAME
    `resolve_faker_pool_identity` the oracle handler uses (HIGH 1), so the
    two routes can never build different pools for what should be one
    identity. `registry` is the caller's provider registry; omitted, the default
    registry is used.
    """
    builder = PoolBuilder(registry if registry is not None else get_default_registry())
    pools_by_column: dict[str, ValuePool] = {}
    for name, col_seed in col_seed_by_name.items():
        if col_seed.strategy != "faker":
            continue
        provider = col_seed.provider
        if provider is None:  # pragma: no cover - admission requires a provider
            raise AssertionError(
                f"native route admitted faker column {name!r} with no provider; "
                "admission should have excluded this."
            )
        cfg = provider_config_to_dict(col_seed.provider_config)
        pool_size, locale, build_config, identity = resolve_faker_pool_identity(
            builder=builder,
            provider=provider,
            plan_pool_size=col_seed.pool_size,
            namespace=col_seed.namespace,
            job_seed=job_seed,
            cfg=cfg,
        )
        cached = pool_cache.get(identity)
        pool = cached if isinstance(cached, ValuePool) else None
        if pool is None:
            pool = builder.build(
                provider=provider,
                size=pool_size,
                job_seed=job_seed,
                locale=locale,
                config=build_config,
                namespace=col_seed.namespace,
            )
            pool_cache.put(pool)
        pools_by_column[name] = pool
    return pools_by_column
