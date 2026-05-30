"""PoolSampler: vectorized sampling from a ValuePool.

Two paths per S5 spec §5:
- Deterministic: per-row `derive_index(seed, namespace, canonical_source, pool_size)`.
- Non-deterministic: `np.random.default_rng(seed_int)` per the NEP-19 contract.

Null preservation: positions where source[i] is null produce null in
output. Sampling counts only non-null positions; saves work on sparse
PII columns.

Cardinality-mode dispatch (S5 §5 + §6 R6 matrix):
- REUSE: random/deterministic indices with replacement.
- UNIQUE: random/deterministic indices without replacement; n <= pool.size.
- MATCH_SOURCE_CARDINALITY: source.nunique() distinct pool entries; stable mapping.
- SCALE_SOURCE_CARDINALITY: same with scale factor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive_index
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._cardinality import CardinalityMode
from decoy_engine.generation.pool._errors import GenerationError

if TYPE_CHECKING:
    from decoy_engine.generation.composite._bundle_pool import BundlePool
    from decoy_engine.generation.pool._value_pool import ValuePool


def _seed_bytes_to_int(seed: bytes) -> int:
    """Convert the 8-byte pool seed to a uint64 for numpy default_rng.

    Mirror image of the S3 spec convention; never used as a determinism
    envelope input (this seed feeds the build-side RNG only).
    """
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
            GenerationError(code='uniqueness_impossible') if UNIQUE and n > pool.size.
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
            return self._deterministic(pool, n, source, seed, namespace)

        # Non-deterministic dispatch.
        rng = np.random.default_rng(_seed_bytes_to_int(seed))
        if mode is CardinalityMode.REUSE:
            indices = rng.integers(0, pool.size, size=n)
            output = pool.values[indices]
            return pd.Series(output)
        if mode is CardinalityMode.UNIQUE:
            if n > pool.size:
                raise GenerationError(
                    code="uniqueness_impossible",
                    message=(
                        f"UNIQUE-mode sample of size {n} from pool of size "
                        f"{pool.size}: cannot draw without replacement."
                    ),
                )
            indices = rng.permutation(pool.size)[:n]
            return pd.Series(pool.values[indices])
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
        """Per-row derive_index path with null preservation.

        S21 Q6 fix (2026-05-30): batch-materialize source + null mask to plain
        Python lists once, then iterate. The prior implementation called
        `source.iloc[i]` + `is_null.iloc[i]` once per row, paying pandas
        scalar-unboxing overhead on every iteration. The HMAC inside
        `derive_index` is the irreducible cost; the pandas overhead is not.
        On a 100K-row column the loop now spends ~half the wall time it did,
        and the savings scale linearly. QA report Q6 + ISO/IEC 25010 §5.2.2
        (performance efficiency).
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
        # One C-level materialization each; replaces 2n `.iloc` calls below.
        src_values = source.tolist()
        is_null_arr = source.isna().to_numpy()
        pool_values = pool.values
        pool_size = pool.size
        output: list[Any] = [pd.NA] * n
        for i, value in enumerate(src_values):
            if is_null_arr[i]:
                continue
            canonical = _canonicalize_source(value)
            idx = derive_index(
                seed=seed,
                namespace=namespace,
                source=canonical,
                pool_size=pool_size,
            )
            output[i] = pool_values[idx]
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
            if len(source) != n:
                raise GenerationError(
                    code="source_length_mismatch",
                    message=(
                        f"sample_bundle deterministic called with source length "
                        f"{len(source)} but n={n}; they must match."
                    ),
                )
            per_col: dict[str, list[Any]] = {c: [] for c in cols}
            is_null = source.isna()
            for i in range(n):
                if is_null.iloc[i]:
                    for c in cols:
                        per_col[c].append(pd.NA)
                    continue
                canonical = _canonicalize_source(source.iloc[i])
                idx = derive_index(
                    seed=seed,
                    namespace=namespace,
                    source=canonical,
                    pool_size=pool.size,
                )
                bundle = pool.values[idx]
                for j, c in enumerate(cols):
                    per_col[c].append(bundle[j])
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
