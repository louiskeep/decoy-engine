"""Per-chunk native masking (compiled kernels + faker pool selection).

Split out of `_dispatch.py` (module-size ratchet, native-throughput program):
these functions do the actual per-chunk-column masking work once a table has
already been admitted to the native route, while `_dispatch` owns the
PREFLIGHT route decision (admission, evidence, the oracle/native fork). The
dependency is one-directional -- `_dispatch` imports these functions back --
so this module must never import `_dispatch` at runtime (that would be a
cycle); the `NativeRouteEvidence` type whose counters it mutates is imported
only under `TYPE_CHECKING` for annotations, and mutated here via plain
attribute access.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution.native._kernels_keyed import native_keyed_hash
from decoy_engine.execution.native._kernels_scalar import (
    native_passthrough,
    native_redact,
    native_truncate,
)
from decoy_engine.generation.pool import GenerationError, PoolBuilder, PoolCache, ValuePool
from decoy_engine.generation.pool._identity import resolve_faker_pool_identity
from decoy_engine.providers_v2 import get_default_registry

if TYPE_CHECKING:
    from decoy_engine.execution.native._dispatch import NativeRouteEvidence
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel


def _resolve_truncate_keep(cfg: dict[str, Any]) -> str:
    """Resolve the legacy `from_end` key to `keep` the way `TruncateHandler.run`
    does: an explicit `keep` wins; otherwise `from_end` maps tail/head.

    This is only the from_end->keep RESOLUTION, not the config VALIDATION: an
    invalid `keep` is rejected upstream at admission (Task 2.6's
    `truncate_config_rejection`, which reroutes the table before it reaches here)
    and again by `native_truncate` itself, so a bad value never reaches this
    admitted-only path.
    """
    keep = cfg.get("keep")
    if keep is not None:
        return keep
    return "tail" if bool(cfg.get("from_end", False)) else "head"


def _sample_faker_chunk(
    source: pa.Array | pa.ChunkedArray,
    *,
    pool: ValuePool,
    col_seed: Any,
    mask_key: bytes | None,
    index_kernel: IndexDerivationKernel,
    native_threads: int | None,
) -> pa.Array:
    """Select one chunk's faker values from the already-built `pool` via the
    preflight-verified compiled index kernel (Task 2.3 Phase 3).

    Reproduces `FakerStrategyHandler.run`'s deterministic-reuse selection
    exactly, scoped to the ONE JC-5-admitted variant
    (`faker_pool_precondition_met` already proved `deterministic=True`,
    `cardinality_mode=reuse`, a namespace, and a pool_size before this table
    reached the native route): `select_seed` is always `mask_key` (the DE-02
    seam re-keys deterministic selection onto the keyed IKM; pool BUILD stays
    on job_seed, resolved once before the chunk loop by `_resolve_faker_pools`).
    `pool_size` is `pool.size` -- the pool as actually BUILT -- never
    `col_seed.pool_size` (the compiled config value the build may have
    resolved differently), matching the oracle's own `PoolSampler._deterministic`.

    One batch call derives every row's pool index; the gather is then a
    null-safe NumPy lookup into `pool.values`, never a per-row Python loop.
    Null handling is POSITIONAL: nulls in `source` restore to `None` at the
    exact same position, never label-aligned (an Arrow column carries no
    label index to misalign with in the first place).
    """
    if mask_key is None:  # pragma: no cover - require_mask_key never returns None
        raise AssertionError(
            "faker pool selection reached with mask_key=None; require_mask_key "
            "always resolves a concrete key before the native route dispatches."
        )
    col = source.combine_chunks() if isinstance(source, pa.ChunkedArray) else source
    n = len(col)
    idx = index_kernel.derive_index_batch(
        col,
        mask_key=mask_key,
        namespace=col_seed.namespace,
        pool_size=pool.size,
        native_threads=native_threads,
    )

    # Runtime invariants on the kernel's own result: a malformed compiled (or
    # stub, in tests) kernel must fail HERE, coded and fail-closed, never as
    # an uncoded NumPy/Arrow out-of-bounds exception from the gather below.
    if len(idx) != n:
        raise GenerationError(
            code="index_batch_length_mismatch",
            message=f"derive_index_batch returned {len(idx)} indices for {n} input rows",
        )
    if idx.type != pa.uint64():
        raise GenerationError(
            code="index_batch_type_mismatch",
            message=f"derive_index_batch returned dtype {idx.type}, expected uint64",
        )
    idx_valid = idx.is_valid().to_numpy(zero_copy_only=False)
    col_valid = col.is_valid().to_numpy(zero_copy_only=False)
    if not np.array_equal(idx_valid, col_valid):
        raise GenerationError(
            code="index_batch_null_mask_mismatch",
            message="derive_index_batch's null positions do not match the source column's",
        )
    idx_np = idx.fill_null(0).to_numpy(zero_copy_only=False)
    if idx_valid.any() and int(idx_np[idx_valid].max()) >= pool.size:
        raise GenerationError(
            code="index_batch_out_of_bounds",
            message=(
                f"derive_index_batch returned an index >= pool_size {pool.size}; "
                "refusing to gather from the pool with it"
            ),
        )

    # Null-safe NumPy gather (no dedup, raw pool order preserved -- pool.values
    # may be gathered with repeats): scatter valid selections positionally,
    # leave null positions as None.
    out = np.empty(n, dtype=object)
    out[idx_valid] = pool.values[idx_np[idx_valid]]
    out[~idx_valid] = None
    return pa.array(out, type=pa.string())


def _mask_chunk_native(
    chunk: pa.Table,
    *,
    col_seed_by_name: dict[str, Any],
    mask_key: bytes | None,
    evidence: NativeRouteEvidence,
    pool_by_column: dict[str, ValuePool],
    native_threads: int | None = None,
    index_kernel: IndexDerivationKernel | None = None,
) -> pa.Table:
    """Mask one chunk column-by-column through the admitted native kernels.

    Every column name in `chunk` is guaranteed present in `col_seed_by_name` by
    the caller's admission precondition (`run_native_or_oracle_chunked` rejects a
    table with any uncovered column at preflight), so a missing lookup here is a
    precondition violation, not a data-shape surprise. `pool_by_column` is
    populated once, before the chunk loop, for every admitted faker column
    (Task 3.1 Step 2); a faker column always has an entry by the same
    precondition. `index_kernel` is the preflight-verified compiled index
    kernel (Task 2.3): non-`None` whenever the admitted table has a faker
    column, since preflight's index probe already ran before this ever executes.
    """
    arrays: dict[str, pa.Array] = {}
    for name in chunk.schema.names:
        col_seed = col_seed_by_name[name]
        strategy = col_seed.strategy
        cfg = provider_config_to_dict(col_seed.provider_config)
        source = chunk.column(name)
        t0 = time.perf_counter()
        if strategy == "passthrough":
            arrays[name] = native_passthrough(source)
        elif strategy == "redact":
            arrays[name] = native_redact(source, redact_with=cfg.get("redact_with", "REDACTED"))
        elif strategy == "truncate":
            # Admission (`truncate_config_rejection`) already proved `length` is
            # a valid positive int before this table reached the native route;
            # `native_truncate` re-validates it anyway (defense in depth).
            length = cfg.get("length")
            arrays[name] = native_truncate(
                source,
                length=length if isinstance(length, int) else 0,
                keep=_resolve_truncate_keep(cfg),
                mask_char=cfg.get("mask_char"),
            )
        elif strategy == "hash":
            arrays[name] = native_keyed_hash(
                source,
                mask_key=mask_key,
                namespace=col_seed.namespace,
                truncate=cfg.get("truncate"),
                native_threads=native_threads,
            )
            # native_keyed_hash never falls back to the pure-Python reference
            # (see _kernels_keyed.py); a successful call IS the compiled kernel.
            evidence.compiled_kernel_executed = True
        elif strategy == "faker":
            if index_kernel is None:  # pragma: no cover - admission implies a loaded kernel
                raise AssertionError(
                    f"native route admitted faker column {name!r} with no index_kernel; "
                    "preflight's index probe should have loaded one for any admitted "
                    "faker node."
                )
            arrays[name] = _sample_faker_chunk(
                source,
                pool=pool_by_column[name],
                col_seed=col_seed,
                mask_key=mask_key,
                index_kernel=index_kernel,
                native_threads=native_threads,
            )
            evidence.pool_select_executed = True
            evidence.pool_select_calls += 1
        else:  # pragma: no cover - preflight admission already excludes this
            raise AssertionError(
                f"native route admitted column {name!r} with strategy {strategy!r}, "
                "which is outside NATIVE_KERNEL_STRATEGIES and NATIVE_POOL_STRATEGIES; "
                "the preflight admission check should have excluded this table."
            )
        evidence.kernel_calls[strategy] = evidence.kernel_calls.get(strategy, 0) + 1
        evidence.kernel_elapsed_s[strategy] = evidence.kernel_elapsed_s.get(strategy, 0.0) + (
            time.perf_counter() - t0
        )
    return pa.table(arrays)


def _resolve_faker_pools(
    col_seed_by_name: dict[str, Any], *, job_seed: bytes, pool_cache: PoolCache
) -> dict[str, ValuePool]:
    """Build/fetch every admitted faker column's pool ONCE, before the chunk
    loop (Task 3.1 Step 2). Keyed by unique `PoolIdentity`, not by column: two
    columns sharing provider + locale + config + namespace share one pool and
    one `pool_cache` entry, matching `FakerStrategyHandler`'s own per-chunk
    cache consult on the oracle side. Uses the SAME
    `resolve_faker_pool_identity` the oracle handler uses (HIGH 1), so the
    two routes can never build different pools for what should be one
    identity.
    """
    registry = get_default_registry()
    builder = PoolBuilder(registry)
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
