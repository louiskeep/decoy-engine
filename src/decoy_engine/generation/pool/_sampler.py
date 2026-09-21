"""PoolSampler: vectorized sampling from a ValuePool.

Two paths per S5 spec §5:
- Deterministic: one batched `derive_index_batch(source, mask_key=seed,
  namespace, pool_size)` over the non-null source values (`_derive_pool_indices`),
  byte-identical to the former per-row `derive_index(seed, namespace,
  canonical_source, pool_size)` loop.
- Non-deterministic: `np.random.default_rng(seed_int)` per the NEP-19 contract.

Null preservation: positions where source[i] is null produce null in
output. Sampling counts only non-null positions; saves work on sparse
PII columns.

Cardinality-mode dispatch (S5 §5 + §6 R6 matrix):
- REUSE: random/deterministic indices with replacement.
- UNIQUE: random/deterministic indices without replacement; requires
  pool.size >= non-null output rows (DE-11).
- MATCH_SOURCE_CARDINALITY: source.nunique() distinct pool entries; stable mapping.
- SCALE_SOURCE_CARDINALITY: same with scale factor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.generation.pool._capacity import unique_capacity_ok
from decoy_engine.generation.pool._cardinality import CardinalityMode
from decoy_engine.generation.pool._errors import GenerationError

if TYPE_CHECKING:
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel
    from decoy_engine.generation.composite._bundle_pool import BundlePool
    from decoy_engine.generation.pool._value_pool import ValuePool


# The compiled index kernel imports back into this package (it reuses
# `_canonicalize_source` + `derive_index`), so resolve it lazily and once to
# keep the module import graph acyclic and the load-time self-test off the
# per-call path.
_COMPILED_INDEX_KERNEL: IndexDerivationKernel | None = None
_COMPILED_INDEX_KERNEL_LOADED = False
_REFERENCE_INDEX_KERNEL: IndexDerivationKernel | None = None


def _compiled_index_kernel() -> IndexDerivationKernel | None:
    """The compiled `derive_index_batch` kernel if the native companion is
    installed, else None. Selected once. The reference kernel below is a
    byte-identical fallback for the None case (the index KAT proves equality),
    so the sampler is correct and slower without the companion, faster with it.
    """
    global _COMPILED_INDEX_KERNEL, _COMPILED_INDEX_KERNEL_LOADED
    if not _COMPILED_INDEX_KERNEL_LOADED:
        from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
        from decoy_engine.execution.native._index_ext import load_compiled_index_kernel

        try:
            _COMPILED_INDEX_KERNEL = load_compiled_index_kernel()
        except CryptoExtensionUnavailableError:
            _COMPILED_INDEX_KERNEL = None
        _COMPILED_INDEX_KERNEL_LOADED = True
    return _COMPILED_INDEX_KERNEL


def _reference_index_kernel() -> IndexDerivationKernel:
    """The pure-Python reference derivation: byte-identical to the former per-row
    loop by construction (same `_canonicalize_source` + `derive_index`)."""
    global _REFERENCE_INDEX_KERNEL
    if _REFERENCE_INDEX_KERNEL is None:
        from decoy_engine.execution.native._index_ext import reference_index_derivation

        _REFERENCE_INDEX_KERNEL = reference_index_derivation()
    return _REFERENCE_INDEX_KERNEL


def _validated_index_array(idx: Any, *, expected_len: int, pool_size: int) -> np.ndarray[Any, Any]:
    """Fail-closed checks on the kernel's own result before it indexes a pool.

    Adapted from the native masking gather (`execution/native/_chunk_masking.
    sample_faker_array`): a malformed compiled (or stub, in tests) kernel must
    fail HERE, coded, never gather from the pool with a bad index. The sampler
    strips nulls before the call, so a valid result carries no nulls and has one
    index per non-null row; the null-position check the masking path runs is a
    null_count check here instead.
    """
    if not isinstance(idx, pa.Array) or idx.type != pa.uint64():
        got = idx.type if isinstance(idx, pa.Array) else type(idx).__name__
        raise GenerationError(
            code="index_batch_type_mismatch",
            message=f"derive_index_batch returned {got}, expected a uint64 Arrow array",
        )
    if len(idx) != expected_len:
        raise GenerationError(
            code="index_batch_length_mismatch",
            message=f"derive_index_batch returned {len(idx)} indices for {expected_len} non-null rows",
        )
    if idx.null_count:
        raise GenerationError(
            code="index_batch_null_mask_mismatch",
            message="derive_index_batch returned a null index for a non-null source value",
        )
    idx_np: np.ndarray[Any, Any] = idx.to_numpy(zero_copy_only=False)
    if expected_len and int(idx_np.max()) >= pool_size:
        raise GenerationError(
            code="index_batch_out_of_bounds",
            message=(
                f"derive_index_batch returned an index >= pool_size {pool_size}; "
                "refusing to gather from the pool with it"
            ),
        )
    return idx_np


def _derive_pool_indices(
    nonnull_source: pd.Series, *, seed: bytes, namespace: str, pool_size: int
) -> np.ndarray[Any, Any]:
    """One pool index per non-null source value, row order preserved.

    Batches the per-row `derive_index` into a single `derive_index_batch` call.
    The compiled kernel takes a native Arrow array built with
    `pa.Array.from_pandas` (the throughput win, and dtype-faithful: it keeps
    `timestamp[ns]` where `pa.array(list)` would truncate to microseconds). Any
    value class the compiled kernel does not admit (dates, decimals,
    magnitudes past int64, mixed-type object columns) falls back to the
    reference kernel over the raw Python values, byte-identical to the former
    per-row loop. The reference is fed the raw values, never an Arrow array: a
    round-trip through Arrow can canonicalize differently (decimal scale,
    sub-microsecond timestamps), while the compiled kernel canonicalizes the
    admitted Arrow types identically to the per-row path (the index KAT).
    """
    raw_values = nonnull_source.tolist()
    compiled = _compiled_index_kernel()
    if compiled is not None:
        try:
            arrow_values: pa.Array | None = pa.Array.from_pandas(nonnull_source)
        except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError, OverflowError):
            # A magnitude past int64 or a mixed-type object column cannot become
            # one native Arrow array; the reference handles it over raw values.
            arrow_values = None
        if arrow_values is not None:
            try:
                idx = compiled.derive_index_batch(
                    arrow_values, mask_key=seed, namespace=namespace, pool_size=pool_size
                )
                return _validated_index_array(
                    idx, expected_len=len(raw_values), pool_size=pool_size
                )
            except GenerationError as exc:
                # The compiled kernel rejects every un-admitted Arrow type with
                # one code; the reference derives those value classes directly
                # (or raises the same per-value error the per-row path raised).
                if exc.code != "native_type_not_admitted":
                    raise
    idx = _reference_index_kernel().derive_index_batch(
        raw_values, mask_key=seed, namespace=namespace, pool_size=pool_size
    )
    return _validated_index_array(idx, expected_len=len(raw_values), pool_size=pool_size)


def _seed_bytes_to_int(seed: bytes) -> int:
    """Convert the 8-byte pool seed to a uint64 for numpy default_rng.

    Mirror image of the S3 spec convention; never used as a determinism
    envelope input (this seed feeds the build-side RNG only).

    GP2 (Codex round-3 spec B): the non-deterministic dispatch previously
    accepted any length here and let a too-short/too-long seed silently
    truncate or overflow through int.from_bytes. A wrong-length seed is a
    determinism-envelope bug, not a value to tolerate -- raise instead.
    """
    if len(seed) != 8:
        raise GenerationError(
            code="invalid_seed_length",
            message=(
                f"PoolSampler non-deterministic seed must be exactly 8 bytes; got {len(seed)}."
            ),
        )
    return int.from_bytes(seed, "big")


class PoolSampler:
    """Sample from a ValuePool under a CardinalityMode.

    Stateless; pool is passed in per call. Tests instantiate fresh; the
    sampler holds no caches.
    """

    def sample(
        self,
        pool: ValuePool,
        n: int,
        *,
        mode: CardinalityMode,
        seed: bytes,
        source: pd.Series | None = None,
        namespace: str | None = None,
        deterministic: bool = False,
        scale: float = 2.0,
    ) -> pd.Series:
        """Return a length-n pd.Series sampled from pool under mode.

        Args:
            pool: the ValuePool to sample from.
            n: output length.
            mode: cardinality mode (REUSE / UNIQUE / MATCH_* / SCALE_*).
            seed: 8 bytes; for deterministic mode this is the row seed
                used by derive_index. For non-deterministic mode it
                seeds default_rng.
            source: pd.Series of source values (required for deterministic
                mode and for MATCH/SCALE modes).
            namespace: required when deterministic=True.
            deterministic: per-column flag from the R6 plan field.
            scale: SCALE_SOURCE_CARDINALITY multiplier (default 2.0).

        Raises:
            GenerationError(code='deterministic_requires_source_and_namespace')
                if deterministic=True with source or namespace missing.
            GenerationError(code='deterministic_mode_unsupported_cardinality')
                if deterministic=True with mode=UNIQUE (QA-1 H9, 2026-06-01).
                The deterministic path keys output on source row identity,
                which means N source rows with the same value get the same
                output value; that's incompatible with UNIQUE which
                requires N distinct outputs. mode=MATCH_SOURCE_CARDINALITY
                with deterministic=True returns REUSE semantics (until a
                future sprint adds a deterministic-source-cardinality
                mode).
            GenerationError(code='uniqueness_impossible') if UNIQUE and the
                non-null output-row count exceeds pool.size (DE-11).
        """
        if deterministic:
            if source is None or namespace is None:
                raise GenerationError(
                    code="deterministic_requires_source_and_namespace",
                    message=(
                        "deterministic=True requires both `source` and `namespace`; "
                        f"got source={'set' if source is not None else 'None'}, "
                        f"namespace={namespace!r}."
                    ),
                )
            # QA-1 H9 (2026-06-01): reject the impossible combo. Pre-fix
            # this branch fell through to _deterministic which produces
            # REUSE semantics (silently ignoring the UNIQUE constraint).
            if mode is CardinalityMode.UNIQUE:
                raise GenerationError(
                    code="deterministic_mode_unsupported_cardinality",
                    message=(
                        "deterministic=True is incompatible with mode=UNIQUE: "
                        "the deterministic path keys output on source row identity, "
                        "so identical source values map to identical outputs (which "
                        "violates UNIQUE). Use deterministic=True with mode=REUSE, "
                        "or mode=UNIQUE with deterministic=False."
                    ),
                )
            return self._deterministic(pool, n, source, seed, namespace)

        # Non-deterministic dispatch.
        rng = np.random.default_rng(_seed_bytes_to_int(seed))
        if mode is CardinalityMode.REUSE:
            indices = rng.integers(0, pool.size, size=n)
            output = pool.values[indices]
            return pd.Series(output)
        if mode is CardinalityMode.UNIQUE:
            # DE-11: UNIQUE capacity is the NON-NULL output-row count. Preserved
            # source nulls are re-emitted as null and consume no pool value, so
            # a null-bearing column needs only as many distinct values as it has
            # non-null rows -- not `n`. Sizing on `n` over-rejected null-bearing
            # columns and disagreed with the compile-time check. When no source
            # is supplied (direct callers/tests), every position is non-null, so
            # the requirement collapses back to `n` and the output is unchanged.
            if source is None:
                nonnull_mask = None
                required = n
            else:
                if len(source) != n:
                    raise GenerationError(
                        code="source_length_mismatch",
                        message=(
                            f"UNIQUE sample called with source length {len(source)} "
                            f"but n={n}; they must match."
                        ),
                    )
                nonnull_mask = source.notna().to_numpy()
                required = int(nonnull_mask.sum())
            # UNIQUE requires distinct OUTPUT values, so capacity and the draw
            # must be on the pool's DISTINCT value set, not its raw size/indices.
            # PoolBuilder does not dedup -- a provider can collide -- so a pool
            # may hold duplicate values; drawing distinct INDICES from such a
            # pool would silently emit duplicates and violate UNIQUE. Dedup is
            # stable (first-seen order), so for an all-distinct pool this is
            # byte-identical to the previous index draw. (Codex HIGH-2, 2026-07-14.)
            distinct_values = pd.unique(pool.values)
            if not unique_capacity_ok(len(distinct_values), required):
                raise GenerationError(
                    code="uniqueness_impossible",
                    message=(
                        f"UNIQUE-mode sample needs {required} distinct value(s) "
                        f"(non-null output rows) but the pool has only "
                        f"{len(distinct_values)} distinct value(s): cannot draw "
                        "without replacement."
                    ),
                )
            drawn = distinct_values[rng.permutation(len(distinct_values))[:required]]
            if nonnull_mask is None:
                return pd.Series(drawn)
            # Scatter the distinct draws into non-null positions; null-source
            # rows stay null (uniform null contract with the deterministic and
            # MATCH/SCALE paths).
            output = np.empty(n, dtype=object)
            output[:] = pd.NA
            output[nonnull_mask] = drawn
            return pd.Series(output)
        if mode is CardinalityMode.MATCH_SOURCE_CARDINALITY:
            return self._match_source_cardinality(pool, n, source, rng, scale=1.0)
        if mode is CardinalityMode.SCALE_SOURCE_CARDINALITY:
            return self._match_source_cardinality(pool, n, source, rng, scale=scale)
        raise GenerationError(
            code="unknown_cardinality_mode",
            message=f"CardinalityMode {mode!r} is not handled by PoolSampler.",
        )

    def _deterministic(
        self,
        pool: ValuePool,
        n: int,
        source: pd.Series,
        seed: bytes,
        namespace: str,
    ) -> pd.Series:
        """Batched derive_index path with positional null preservation.

        The per-row `derive_index` loop is replaced by ONE `derive_index_batch`
        call over the non-null source values (`_derive_pool_indices`), the same
        drop-in that made Phase 2 pool selection ~13x on the masking route. The
        HMAC that was the irreducible per-row cost is now batched into the
        compiled kernel (or the byte-identical Python reference when the native
        companion is absent). Null handling stays POSITIONAL: `source.isna()`
        rows re-emit `pd.NA`, consume no index, and the surviving rows keep their
        order. Output is byte-identical to the former per-row path (proven by the
        legacy-oracle differential tests).
        """
        if len(source) != n:
            # Caller error: source length must match n; this is a
            # contract surface, raise loudly.
            raise GenerationError(
                code="source_length_mismatch",
                message=(
                    f"deterministic sample called with source length {len(source)} "
                    f"but n={n}; they must match for per-row determinism."
                ),
            )
        is_null = source.isna().to_numpy()
        output: list[Any] = [pd.NA] * n
        nonnull_positions = np.flatnonzero(~is_null)
        if nonnull_positions.size:
            idx_np = _derive_pool_indices(
                source.iloc[nonnull_positions],
                seed=seed,
                namespace=namespace,
                pool_size=pool.size,
            )
            selected = pool.values[idx_np]
            for pos, value in zip(nonnull_positions.tolist(), selected, strict=True):
                output[pos] = value
        return pd.Series(output)

    def _match_source_cardinality(
        self,
        pool: ValuePool,
        n: int,
        source: pd.Series | None,
        rng: np.random.Generator,
        scale: float,
    ) -> pd.Series:
        """MATCH or SCALE cardinality mode.

        Picks `target_distinct = source.nunique() * scale` pool values, then
        maps each distinct source value to one of them. The mapping is keyed
        to the sorted distinct-value set (NF3), so it is independent of source
        row order and reproducible across processes.
        """
        if source is None:
            raise GenerationError(
                code="source_required_for_cardinality_mode",
                message=(
                    "MATCH_SOURCE_CARDINALITY / SCALE_SOURCE_CARDINALITY require a non-None source."
                ),
            )
        source_distinct = int(source.dropna().nunique())
        target_distinct = max(1, round(source_distinct * scale))
        if target_distinct > pool.size:
            raise GenerationError(
                code="cardinality_target_exceeds_pool",
                message=(
                    f"Target distinct {target_distinct} (source.nunique() {source_distinct} "
                    f"* scale {scale}) exceeds pool.size {pool.size}."
                ),
            )
        # Pick target_distinct distinct pool values.
        chosen_indices = rng.permutation(pool.size)[:target_distinct]
        chosen_pool_values = pool.values[chosen_indices]
        # NF3: order the distinct source values canonically (sorted) rather
        # than by row-appearance order. The prior `list(...unique())` keyed the
        # source-value -> pool-value mapping on the order rows happened to
        # arrive, so the same column in a different row order produced a
        # different mapping (the mapping was not a function of the data). Sorting
        # makes it a pure function of the distinct-value SET, stable across row
        # orderings and across processes. If there are fewer chosen pool values
        # than source uniques (scale < 1), the round-robin reuses them.
        source_uniques = sorted(source.dropna().unique())
        value_map = {
            src_val: chosen_pool_values[i % target_distinct]
            for i, src_val in enumerate(source_uniques)
        }
        output: list[Any] = []
        is_null = source.isna()
        for i in range(n):
            if is_null.iloc[i]:
                output.append(pd.NA)
            else:
                output.append(value_map[source.iloc[i]])
        return pd.Series(output)

    def sample_bundle(
        self,
        pool: BundlePool,
        n: int,
        *,
        mode: CardinalityMode,
        seed: bytes,
        source: pd.Series | None = None,
        namespace: str | None = None,
        deterministic: bool = False,
    ) -> dict[str, pd.Series]:
        """Sample n bundle tuples, then explode into one Series per output_column.

        Per S8 spec §3b: the index selection is IDENTICAL to `sample`
        (deterministic: per-row `derive_index` over the canonicalized source,
        with null preservation; non-deterministic: `default_rng`). The only
        bundle-specific work is splitting each selected tuple across the
        pool's `output_columns`. This keeps a composite's determinism path
        byte-for-byte aligned with the scalar sampler (same `derive_index` +
        `_canonicalize_source`), which the cross-sprint coherence contract needs.
        """
        cols = pool.output_columns
        if not cols:
            raise GenerationError(
                code="bundle_missing_output_columns",
                message="sample_bundle requires a BundlePool with non-empty output_columns.",
            )

        if deterministic:
            if source is None or namespace is None:
                raise GenerationError(
                    code="deterministic_requires_source_and_namespace",
                    message=(
                        "deterministic=True requires both `source` and `namespace`; "
                        f"got source={'set' if source is not None else 'None'}, "
                        f"namespace={namespace!r}."
                    ),
                )
            # QA-1 H9 (2026-06-01): reject UNIQUE + deterministic on the
            # bundle path too. See `sample()` for rationale.
            if mode is CardinalityMode.UNIQUE:
                raise GenerationError(
                    code="deterministic_mode_unsupported_cardinality",
                    message=(
                        "sample_bundle: deterministic=True is incompatible with "
                        "mode=UNIQUE. Same constraint as the scalar sample() path."
                    ),
                )
            if len(source) != n:
                raise GenerationError(
                    code="source_length_mismatch",
                    message=(
                        f"sample_bundle deterministic called with source length "
                        f"{len(source)} but n={n}; they must match."
                    ),
                )
            # ONE shared batch-index array per row across every bundle column:
            # the same `derive_index_batch` drop-in as the scalar path, then each
            # column gathers by the SAME index (the cross-column tuple-integrity
            # contract is preserved by construction). Nulls stay positional.
            per_col: dict[str, list[Any]] = {c: [pd.NA] * n for c in cols}
            is_null = source.isna().to_numpy()
            nonnull_positions = np.flatnonzero(~is_null)
            if nonnull_positions.size:
                idx_np = _derive_pool_indices(
                    source.iloc[nonnull_positions],
                    seed=seed,
                    namespace=namespace,
                    pool_size=pool.size,
                )
                pool_values = pool.values
                for pos, ix in zip(nonnull_positions.tolist(), idx_np.tolist(), strict=True):
                    bundle = pool_values[ix]
                    for j, c in enumerate(cols):
                        per_col[c][pos] = bundle[j]
            return {c: pd.Series(per_col[c]) for c in cols}

        # Non-deterministic: with-replacement by default; UNIQUE without.
        rng = np.random.default_rng(_seed_bytes_to_int(seed))
        if mode is CardinalityMode.UNIQUE:
            if n > pool.size:
                raise GenerationError(
                    code="uniqueness_impossible",
                    message=(
                        f"UNIQUE-mode bundle sample of size {n} from pool of size "
                        f"{pool.size}: cannot draw without replacement."
                    ),
                )
            indices = rng.permutation(pool.size)[:n]
        else:
            indices = rng.integers(0, pool.size, size=n)
        per_col = {c: [] for c in cols}
        for idx in indices:
            bundle = pool.values[idx]
            for j, c in enumerate(cols):
                per_col[c].append(bundle[j])
        return {c: pd.Series(per_col[c]) for c in cols}
