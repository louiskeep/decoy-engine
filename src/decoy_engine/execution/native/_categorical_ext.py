"""Native deterministic categorical selection, reusing `derive_index_batch`.

Phase 5 Track B: the first genuinely new native mask operator. The oracle's
deterministic categorical (`_strategies/_categorical.py`) is SDV-style keyed
selection -- each source value maps to a category via a keyed pool-index draw
(uniform), or a keyed uniform draw over a fixed integer resolution routed
through a weight CDF (weighted). `derive_index_batch` already computes that
keyed draw byte-identically to the oracle's per-row `derive_index`
(KAT-pinned, `_index_ext.py`; canonicalization shared via
`generation.pool._canonicalize._canonicalize_source`), so this operator is a
null-safe NumPy gather over its output -- no new Rust, parity inherited from
an existing KAT family.

Scope (see docs/plans/2026-09-21-phase5-native-operator-expansion.md and
docs/plans/2026-10-04-c1-chunked-categorical.md): STRING categories over a string
source, deterministic mode. Both the full-frame shadow operator and the chunked route
(`_chunk_masking._mask_chunk_native`, one call per chunk) call this function. The weighted
`np.searchsorted(cdf, bucket, side="right")` reproduces the oracle's
`bisect.bisect_right(cdf, bucket)` exactly for the sorted integer CDF (pinned
by a differential test); the runtime invariants below mirror
`_chunk_masking.sample_faker_array` so a malformed compiled (or stub) kernel
fails HERE, coded and fail-closed, never as an uncoded exception.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np
import pyarrow as pa

from decoy_engine.execution._positional_keys import positional_key_array
from decoy_engine.execution._strategies._categorical import _WEIGHTED_CDF_RES
from decoy_engine.generation.pool import GenerationError

if TYPE_CHECKING:
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel


def native_categorical(
    array: pa.Array | pa.ChunkedArray,
    *,
    categories: Sequence[str],
    cdf: Sequence[int] | None,
    mask_key: bytes | None,
    namespace: str,
    index_kernel: IndexDerivationKernel,
    native_threads: int | None = None,
) -> pa.Array:
    """Remap `array` onto `categories` via the compiled index kernel.

    `cdf is None` selects the uniform path (`pool_size = len(categories)`); a
    non-null `cdf` (the oracle's `_build_cdf` output over `_WEIGHTED_CDF_RES`)
    selects the weighted path (`pool_size = _WEIGHTED_CDF_RES`, then a
    vectorized `searchsorted` maps each keyed bucket to a category index).
    Nulls in `array` restore to `None` at the same position, never
    label-aligned. Output is pinned `pa.string()` per batch (stable across
    batches, so the coordinator's part-concat never type-drifts). The full-frame route
    reconciles the whole column's null shape to the oracle's data-dependent type at
    final assembly (`_shadow_assembly.assemble_column`); the chunked route keeps
    `string` on every chunk (`_chunked_schema_rule`).
    """
    if mask_key is None:  # pragma: no cover - require_mask_key never returns None
        raise AssertionError(
            "categorical selection reached with mask_key=None; require_mask_key "
            "always resolves a concrete key before the native route dispatches."
        )
    col = array.combine_chunks() if isinstance(array, pa.ChunkedArray) else array
    pool_size = len(categories) if cdf is None else _WEIGHTED_CDF_RES
    idx = index_kernel.derive_index_batch(
        col,
        mask_key=mask_key,
        namespace=namespace,
        pool_size=pool_size,
        native_threads=native_threads,
    )
    return _select(idx, col.is_valid(), col.is_valid(), categories, cdf, pool_size)


def _select(
    idx: pa.Array,
    input_valid: pa.Array,
    out_valid: pa.Array,
    categories: Sequence[str],
    cdf: Sequence[int] | None,
    pool_size: int,
) -> pa.Array:
    """Validate the kernel's indices and gather the categories.

    `input_valid` is the validity of what the kernel was handed (its null positions must
    come back unchanged); `out_valid` is the validity the output takes. They coincide for
    the source-keyed call and differ for the position-keyed one, whose key column has no
    nulls while the output restores the source's."""
    n = len(out_valid)
    # Runtime invariants on the kernel's own result, mirroring
    # `sample_faker_array`: the isinstance/type check comes FIRST so a
    # non-`pa.Array` result cannot leak an uncoded AttributeError.
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
    idx_valid = idx.is_valid().to_numpy(zero_copy_only=False)
    in_valid = input_valid.to_numpy(zero_copy_only=False)
    if not np.array_equal(idx_valid, in_valid):
        raise GenerationError(
            code="index_batch_null_mask_mismatch",
            message="derive_index_batch's null positions do not match the source column's",
        )
    idx_np = idx.fill_null(0).to_numpy(zero_copy_only=False)
    if idx_valid.any() and int(idx_np[idx_valid].max()) >= pool_size:
        raise GenerationError(
            code="index_batch_out_of_bounds",
            message=(
                f"derive_index_batch returned an index >= pool_size {pool_size}; "
                "refusing to select a category with it"
            ),
        )

    if cdf is None:
        cat_idx = idx_np
    else:
        # np.searchsorted(cdf, bucket, side="right") == bisect.bisect_right for a
        # sorted CDF (differential test pins it). The clamp mirrors the oracle's
        # own defensive `if cat_idx >= len(categories)` guard; a bucket in
        # [0, _WEIGHTED_CDF_RES) never actually exceeds the last band, whose
        # upper bound is _WEIGHTED_CDF_RES, so the clamp is belt-and-suspenders.
        cdf_arr = np.asarray(cdf, dtype=np.int64)
        cat_idx = np.searchsorted(cdf_arr, idx_np.astype(np.int64), side="right")
        np.minimum(cat_idx, len(categories) - 1, out=cat_idx)

    keep = out_valid.to_numpy(zero_copy_only=False)
    categories_arr = np.array(list(categories), dtype=object)
    out = np.empty(n, dtype=object)
    out[keep] = categories_arr[cat_idx[keep]]
    out[~keep] = None
    return pa.array(out, type=pa.string())


def native_categorical_positional(
    array: pa.Array | pa.ChunkedArray,
    *,
    row_offset: int,
    categories: Sequence[str],
    cdf: Sequence[int] | None,
    mask_key: bytes | None,
    namespace: str,
    index_kernel: IndexDerivationKernel,
    native_threads: int | None = None,
) -> pa.Array:
    """Seeded non-deterministic selection, keyed by global row position.

    The draw for local row `i` is the index kernel's draw for the canonical integer
    `row_offset + i`, so it ignores the source value; this reproduces the oracle's
    `derive_index(mask_key, namespace, encode_int(row_offset + i), pool_size)`. The key
    column is a dense `uint64` (the offset domain is `[0, 2**64-1]`, which int64 cannot
    hold) with no nulls, so nulls are restored from the SOURCE and still consume their
    position, exactly as the oracle's `enumerate` does."""
    if mask_key is None:  # pragma: no cover - require_mask_key never returns None
        raise AssertionError("positional categorical reached with mask_key=None")
    col = array.combine_chunks() if isinstance(array, pa.ChunkedArray) else array
    keys = positional_key_array(row_offset, len(col), code="categorical_position_out_of_domain")
    pool_size = len(categories) if cdf is None else _WEIGHTED_CDF_RES
    idx = index_kernel.derive_index_batch(
        keys,
        mask_key=mask_key,
        namespace=namespace,
        pool_size=pool_size,
        native_threads=native_threads,
    )
    return _select(idx, keys.is_valid(), col.is_valid(), categories, cdf, pool_size)


__all__ = ["native_categorical", "native_categorical_positional"]
