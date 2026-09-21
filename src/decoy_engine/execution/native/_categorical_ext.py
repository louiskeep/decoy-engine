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

v1 scope (see docs/plans/2026-09-21-phase5-native-operator-expansion.md):
STRING categories, deterministic mode, FULL-FRAME route only. The weighted
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
    batches, so the coordinator's part-concat never type-drifts); the
    whole-column null-shape reconciliation to the oracle's data-dependent type
    happens at final assembly (`_shadow_coordinator._assemble_column`).
    """
    if mask_key is None:  # pragma: no cover - require_mask_key never returns None
        raise AssertionError(
            "categorical selection reached with mask_key=None; require_mask_key "
            "always resolves a concrete key before the native route dispatches."
        )
    col = array.combine_chunks() if isinstance(array, pa.ChunkedArray) else array
    n = len(col)
    pool_size = len(categories) if cdf is None else _WEIGHTED_CDF_RES
    idx = index_kernel.derive_index_batch(
        col,
        mask_key=mask_key,
        namespace=namespace,
        pool_size=pool_size,
        native_threads=native_threads,
    )

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
    col_valid = col.is_valid().to_numpy(zero_copy_only=False)
    if not np.array_equal(idx_valid, col_valid):
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

    categories_arr = np.array(list(categories), dtype=object)
    out = np.empty(n, dtype=object)
    out[idx_valid] = categories_arr[cat_idx[idx_valid]]
    out[~idx_valid] = None
    return pa.array(out, type=pa.string())


__all__ = ["native_categorical"]
