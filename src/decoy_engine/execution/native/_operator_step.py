"""The one kernel step per native operator, shared by both routes.

Given an operator's resolved parameters and a source, `run_kernel_step` calls the compiled
kernel and reports whether one ran. The unified full-frame route (`physical/_shadow_operators`)
and the chunked route (`_chunk_masking`) both call it, so kernel-argument assembly, the
`namespace or ""` rule and the `derive_calls` reduction exist once. Route contracts stay in the
adapters: evidence flags, companion-unavailable handling, row-error shaping and null casts.

The step never catches `CryptoExtensionUnavailableError`, never touches evidence and never
checks a route precondition. Its index-kernel guards are defensive duplicates of the adapters'.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._positional_keys import positional_key_array
from decoy_engine.execution.native._bucket_perturb_ext import native_bucket_perturb
from decoy_engine.execution.native._categorical_ext import (
    native_categorical,
    native_categorical_positional,
)
from decoy_engine.execution.native._date_shift_ext import native_date_shift
from decoy_engine.execution.native._group_key_kernel import native_group_key
from decoy_engine.execution.native._kernels_keyed import native_keyed_hash
from decoy_engine.execution.native._kernels_scalar import (
    native_passthrough,
    native_redact,
    native_text_redact,
    native_truncate,
)
from decoy_engine.execution.native._operator_params import (
    BucketPerturbParams,
    CategoricalParams,
    FakerParams,
    GroupKeyParams,
    HashParams,
    OperatorParams,
    PassthroughParams,
    RedactParams,
    TextRedactParams,
    TruncateParams,
)
from decoy_engine.generation.pool import GenerationError

if TYPE_CHECKING:
    from decoy_engine.execution.native._group_key_ext import RawHexDerivationKernel
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel
    from decoy_engine.generation.pool import ValuePool

__all__ = [
    "StepResult",
    "run_kernel_step",
    "run_kernel_step_masked",
    "sample_faker_array",
    "sample_faker_array_positional",
]


def sample_faker_array(
    source: pa.Array | pa.ChunkedArray,
    *,
    pool: ValuePool,
    namespace: str,
    mask_key: bytes | None,
    index_kernel: IndexDerivationKernel,
    native_threads: int | None,
) -> pa.Array:
    """Select one batch's faker values from the already-built `pool` via the
    preflight-verified compiled index kernel (Task 2.3 Phase 3).

    Both routes reach this through `run_kernel_step`, so they run the
    IDENTICAL selection code -- never two independently-written copies that
    could drift. `namespace` is passed explicitly (rather than a `col_seed`
    object) since the unified side has only its resolved parameters, not a
    compiled `ColumnSeed`.

    Reproduces `FakerStrategyHandler.run`'s deterministic-reuse selection
    exactly, scoped to the ONE JC-5-admitted variant
    (`faker_pool_precondition_met` already proved `deterministic=True`,
    `cardinality_mode=reuse`, a namespace, and a pool_size before this table
    reached the native route): `select_seed` is always `mask_key` (the DE-02
    seam re-keys deterministic selection onto the keyed IKM; pool BUILD stays
    on job_seed, resolved once before the chunk loop by `_resolve_faker_pools`).
    `pool_size` is `pool.size` -- the pool as actually BUILT -- never the
    compiled config's own pool-size value (the build may have resolved it
    differently), matching the oracle's own `PoolSampler._deterministic`.

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
    idx = _checked_batch(
        index_kernel.derive_index_batch(
            col,
            mask_key=mask_key,
            namespace=namespace,
            pool_size=pool.size,
            native_threads=native_threads,
        ),
        n=n,
    )
    idx_valid = idx.is_valid().to_numpy(zero_copy_only=False)
    col_valid = col.is_valid().to_numpy(zero_copy_only=False)
    if not np.array_equal(idx_valid, col_valid):
        raise GenerationError(
            code="index_batch_null_mask_mismatch",
            message="derive_index_batch's null positions do not match the source column's",
        )
    return _gather_pool_values(pool, idx, idx_valid)


def sample_faker_array_positional(
    source: pa.Array | pa.ChunkedArray,
    *,
    pool: ValuePool,
    row_offset: int,
    job_seed: bytes | None,
    namespace: str,
    index_kernel: IndexDerivationKernel,
    native_threads: int | None,
) -> pa.Array:
    """Select one batch's non-deterministic REUSE faker values by global row position.

    Row `i` draws `pool.values[derive_index(job_seed, namespace, encode_int(row_offset + i),
    pool.size)]`, the oracle's `positional_pool_indices`, so a chunk at any offset reproduces
    the whole-frame draw. `namespace` is the SELECTION namespace; the pool was built from the
    configured one. The key is `job_seed`, never the secret-derived `mask_key`: this mode
    generates fresh values and does not re-identify a source value.

    Unlike `sample_faker_array` the keys are a dense `uint64` column, so the index null mask
    is the keys' (none) and cannot equal the source's. Nulls are restored from the SOURCE and
    still consume their ordinal, as the oracle's `na_mask` does.
    """
    if job_seed is None:  # pragma: no cover - the chunked adapter always passes the job seed
        raise AssertionError("positional faker selection reached with job_seed=None.")
    col = source.combine_chunks() if isinstance(source, pa.ChunkedArray) else source
    n = len(col)
    keys = positional_key_array(row_offset, n, code="faker_position_out_of_domain")
    idx = _checked_batch(
        index_kernel.derive_index_batch(
            keys,
            mask_key=job_seed,
            namespace=namespace,
            pool_size=pool.size,
            native_threads=native_threads,
        ),
        n=n,
    )
    if idx.null_count:
        # Dense uint64 keys have no nulls, so a null index is a malformed kernel, not data.
        raise GenerationError(
            code="index_batch_null_mask_mismatch",
            message="derive_index_batch returned null indices for non-null positional keys",
        )
    return _gather_pool_values(pool, idx, col.is_valid().to_numpy(zero_copy_only=False))


def _checked_batch(idx: object, *, n: int) -> pa.Array:
    """`idx` as the kernel's uint64 index array of length `n`, or a coded error.

    A malformed compiled (or stub, in tests) kernel must fail HERE, coded and fail-closed,
    never as an uncoded exception. The isinstance/type check comes FIRST: a non-`pa.Array`
    result (a bare list, None) has no `.type`/`.is_valid()`, so probing those (or `len`)
    before confirming the shape would leak an uncoded AttributeError instead of the coded
    error every other malformed shape gets."""
    if not isinstance(idx, pa.Array) or idx.type != pa.uint64():
        got = idx.type if isinstance(idx, pa.Array) else type(idx).__name__
        raise GenerationError(
            code="index_batch_type_mismatch",
            message=f"derive_index_batch returned {got}, expected a uint64 Arrow array",
        )
    if len(idx) != n:
        raise GenerationError(
            code="index_batch_length_mismatch",
            message=f"derive_index_batch returned {len(idx)} indices for {n} input rows",
        )
    return idx


def _gather_pool_values(pool: ValuePool, idx: pa.Array, valid: Any) -> pa.Array:
    """The pool values at `idx` where `valid`, null elsewhere.

    A null-safe NumPy gather (no dedup, raw pool order preserved -- pool.values may be gathered
    with repeats): valid selections scatter positionally, null positions stay None."""
    idx_np = idx.fill_null(0).to_numpy(zero_copy_only=False)
    if valid.any() and int(idx_np[valid].max()) >= pool.size:
        raise GenerationError(
            code="index_batch_out_of_bounds",
            message=(
                f"derive_index_batch returned an index >= pool_size {pool.size}; "
                "refusing to gather from the pool with it"
            ),
        )
    out = np.empty(len(idx_np), dtype=object)
    out[valid] = pool.values[idx_np[valid]]
    out[~valid] = None
    return pa.array(out, type=pa.string())


@dataclass(frozen=True)
class StepResult:
    out: pa.Array
    # None: an unkeyed transform, which makes no compiled-kernel claim.
    ran: bool | None
    # Batch-local positions of non-null date_shift values that did not parse.
    format_error_positions: tuple[int, ...] = ()


def _index_kernel_for(
    params: OperatorParams, index_kernel: IndexDerivationKernel | None
) -> IndexDerivationKernel:
    if index_kernel is None:  # pragma: no cover - admission implies a loaded kernel
        raise AssertionError(
            f"{type(params).__name__} reached the kernel step with no index_kernel; "
            "preflight's index probe should have loaded one for any admitted node."
        )
    return index_kernel


def run_kernel_step(
    params: OperatorParams,
    source: pa.Array | pa.ChunkedArray,
    *,
    mask_key: bytes | None,
    native_threads: int | None,
    index_kernel: IndexDerivationKernel | None = None,
    raw_hex_kernel: RawHexDerivationKernel | None = None,
    pool: ValuePool | None = None,
    sibling: pa.Table | None = None,
    row_offset: int = 0,
    job_seed: bytes | None = None,
) -> StepResult:
    """Run one operator's compiled kernel over `source` and say whether it ran.

    `sibling` is group_key's input (the single-column slice of its group_by column) and
    `source` is ignored for it. `row_offset` only matters to the position-keyed categorical and
    the position-keyed faker, which also keys on `job_seed` (never `mask_key`).
    `raw_hex_kernel=None` lets group_key load its own, which the unified route relies on as
    its one companion probe. `ran` for bucket_perturb, group_key and date_shift is each
    kernel's own `derive_calls` total, and those kernels disagree on purpose: group_key counts
    any non-empty sibling (a null cell is stringified and hashed), date_shift and
    bucket_perturb only chunks with a parseable value.
    """
    if isinstance(params, PassthroughParams):
        return StepResult(native_passthrough(source), None)
    if isinstance(params, RedactParams):
        return StepResult(native_redact(source, redact_with=params.redact_with), None)
    if isinstance(params, TruncateParams):
        out = native_truncate(
            source, length=params.length, keep=params.keep, mask_char=params.mask_char
        )
        return StepResult(out, None)
    if isinstance(params, TextRedactParams):
        out = native_text_redact(
            source,
            detectors=params.detectors,
            token=params.token,
            label_token=params.label_token,
        )
        return StepResult(out, None)
    if isinstance(params, HashParams):
        # native_keyed_hash never falls back to the pure-Python reference
        # (see _kernels_keyed.py); a successful call IS the compiled kernel.
        out = native_keyed_hash(
            source,
            mask_key=mask_key,
            namespace=params.namespace,
            truncate=params.truncate,
            native_threads=native_threads,
        )
        return StepResult(out, True)
    if isinstance(params, FakerParams) and params.positional:
        if pool is None or job_seed is None or params.selection_namespace is None:
            # The unified route never builds positional params, and the chunked adapter passes
            # the pool and the job seed for every admitted column.
            raise AssertionError(
                "positional FakerParams reached the kernel step with no pool, job_seed or "
                "selection namespace."
            )
        if len(source) == 0:
            return StepResult(pa.array([], pa.string()), False)
        out = sample_faker_array_positional(
            source,
            pool=pool,
            row_offset=row_offset,
            job_seed=job_seed,
            namespace=params.selection_namespace,
            index_kernel=_index_kernel_for(params, index_kernel),
            native_threads=native_threads,
        )
        return StepResult(out, True)
    if isinstance(params, FakerParams):
        if params.namespace is None or pool is None:  # pragma: no cover - admission requires both
            raise AssertionError("FakerParams reached the kernel step with no namespace or pool.")
        out = sample_faker_array(
            source,
            pool=pool,
            namespace=params.namespace,
            mask_key=mask_key,
            index_kernel=_index_kernel_for(params, index_kernel),
            native_threads=native_threads,
        )
        return StepResult(out, True)
    if isinstance(params, CategoricalParams):
        kernel = _index_kernel_for(params, index_kernel)
        prepared = params.prepared
        if not prepared.positional:
            out = native_categorical(
                source,
                categories=prepared.categories,
                cdf=prepared.cdf,
                mask_key=mask_key,
                namespace=params.namespace or "",
                index_kernel=kernel,
                native_threads=native_threads,
            )
            return StepResult(out, True)
        if len(source) == 0:
            return StepResult(pa.array([], pa.string()), False)
        out = native_categorical_positional(
            source,
            row_offset=row_offset,
            categories=prepared.categories,
            cdf=prepared.cdf,
            mask_key=mask_key,
            namespace=params.namespace or "",
            index_kernel=kernel,
            native_threads=native_threads,
        )
        return StepResult(out, True)
    derive_calls: list[int] = []
    if isinstance(params, BucketPerturbParams):
        out = native_bucket_perturb(
            source,
            bucket=params.bucket,
            date_format=params.date_format,
            mask_key=mask_key,
            namespace=params.namespace or "",
            index_kernel=_index_kernel_for(params, index_kernel),
            native_threads=native_threads,
            derive_calls=derive_calls,
        )
        return StepResult(out, sum(derive_calls) > 0)
    if isinstance(params, GroupKeyParams):
        if sibling is None:  # pragma: no cover - the caller slices the sibling for group_key
            raise AssertionError("GroupKeyParams reached the kernel step with no sibling.")
        out = native_group_key(
            sibling,
            length=params.length,
            prefix=params.prefix,
            mask_key=mask_key,
            namespace=params.namespace,
            native_threads=native_threads,
            raw_hex_kernel=raw_hex_kernel,
            derive_calls=derive_calls,
        )
        return StepResult(out, sum(derive_calls) > 0)
    out, positions = native_date_shift(
        source,
        min_days=params.min_days,
        max_days=params.max_days,
        date_format=params.date_format,
        mask_key=mask_key,
        namespace=params.namespace or "",
        index_kernel=_index_kernel_for(params, index_kernel),
        native_threads=native_threads,
        derive_calls=derive_calls,
    )
    return StepResult(out, sum(derive_calls) > 0, positions)


def run_kernel_step_masked(
    params: OperatorParams,
    source: pa.Array | pa.ChunkedArray,
    mask: pa.Array,
    *,
    mask_key: bytes | None,
    native_threads: int | None,
    index_kernel: IndexDerivationKernel | None = None,
) -> StepResult:
    """`run_kernel_step` for a `when:` column: only the rows `mask` selects take the masked value.

    `mask` is a non-null boolean array over `source`. With no selected row the kernel is not
    called and `source` comes back unchanged with `ran=False`, so the adapter counts nothing
    and credits no compiled backend for the chunk. Otherwise the kernel runs over the selected
    rows only and their outputs are scattered back into place, so the cost follows selectivity.
    For the value-keyed operators over a string source (hash, redact, truncate, deterministic
    categorical) that equals the oracle's run on the selected subset plus its write-back, row by
    row, because each row's output depends only on that row's value and the config. Unselected
    rows, nulls included, keep their source value.
    """
    plain = source.combine_chunks() if isinstance(source, pa.ChunkedArray) else source
    if not (pc.sum(mask).as_py() or 0):
        return StepResult(plain, False)
    result = run_kernel_step(
        params,
        pc.filter(plain, mask),
        mask_key=mask_key,
        native_threads=native_threads,
        index_kernel=index_kernel,
    )
    return StepResult(pc.replace_with_mask(plain, mask, result.out), result.ran)
