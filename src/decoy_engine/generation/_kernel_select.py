"""Position-keyed index selection for the unified generation core (Phase F FR3).

The core's uniform and weighted selections (pooled Faker, categorical, reference) draw their
indices from the shared HMAC index kernel instead of a stateful Python/NumPy RNG. Row `g` of a
column takes `derive_index(column_key, namespace, encode_int(g), pool_size)`, the same position-
keyed shape masking's non-deterministic Faker uses (`execution/_strategies/_faker_positional.py`).
A draw is therefore a pure function of the column key and the GLOBAL row number: it needs no
carried RNG state, so how rows are cut into batches cannot change a value, and the compiled kernel
(or its byte-identical Python reference when the companion is absent) can run a whole batch at once.

Distribution is preserved by construction, not by testing alone:

- uniform over `n` choices: `index = draw mod n`. For `n <= 2**56` the modulo bias is at most
  `n / 2**64` (`determinism.derive_index`), far below any sampling noise;
- weighted: inverse-transform sampling. One draw over `2**32` equally likely integers is looked up
  in the cumulative-weight thresholds with a right bisect, so category `i` owns exactly the slice
  `[T[i-1], T[i])` of the draw space. `2**32` divides `2**64`, so the integer draw is exactly
  uniform and each probability is off by at most `2**-32` from the configured weight. Masking's
  deterministic weighted categorical uses the same CDF-over-a-uniform-integer shape
  (`execution/_strategies/_categorical._build_cdf`, resolution 1e6); generation takes a finer
  resolution because it has no compatibility constraint with an older golden.

A weight set this mapping cannot represent faithfully (a length mismatch, a non-finite or negative
weight, a zero total, or a positive weight too small to own one slot) is declined, and the stream
keeps the Python RNG for that column. Declining is a function of the configuration alone, so the
choice of backend never depends on the host, and a distribution is never flattened to fit the
kernel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa

from decoy_engine.generation.pool import _sampler

if TYPE_CHECKING:
    from collections.abc import Sequence

    from decoy_engine.generators.derivation import GenDeriveContext

# Disjoint HMAC domain off the column root, so the selection key can never equal a build,
# Python-RNG, NumPy or Faker key of the same column.
SELECT_FAMILY = "kernel_select/v1"
UNIFORM_NAMESPACE = "gen/select/uniform"
WEIGHTED_NAMESPACE = "gen/select/weighted"

# Weighted draws are uniform integers in [0, CDF_RESOLUTION). A power of two dividing 2**64, so
# the modulo that derives them from the 64-bit HMAC prefix introduces no bias at all.
CDF_RESOLUTION = 2**32
# `derive_index` refuses pools above this (determinism._derive._POOL_SIZE_MAX).
MAX_POOL = 2**56
_UINT64_MAX = 2**64 - 1
_DOMAIN_CODE = "generation_row_out_of_domain"


@dataclass(frozen=True)
class PositionalIndexer:
    """The index kernel call for one column: key, namespace and the pool size it draws over."""

    key: bytes
    namespace: str
    pool_size: int

    def draw(self, offset: int, size: int) -> np.ndarray[Any, Any]:
        """One index in `[0, pool_size)` per global row `offset .. offset + size - 1`.

        The kernel is looked up on every call through `_sampler`, the one place that picks the
        compiled kernel when the companion is installed and the byte-identical reference
        otherwise, so a column's values are the same on every host.
        """
        if size == 0:
            return np.empty(0, dtype=np.uint64)
        if offset < 0 or offset + size - 1 > _UINT64_MAX:
            from decoy_engine.generation.pool._errors import GenerationError

            raise GenerationError(
                code=_DOMAIN_CODE,
                message=f"rows [{offset}, {offset + size}) leave the uint64 position domain",
            )
        keys = pa.array(np.arange(offset, offset + size, dtype=np.uint64), pa.uint64())
        kernel = _sampler._compiled_index_kernel() or _sampler._reference_index_kernel()
        idx = kernel.derive_index_batch(
            keys, mask_key=self.key, namespace=self.namespace, pool_size=self.pool_size
        )
        return _sampler._validated_index_array(idx, expected_len=size, pool_size=self.pool_size)


def selection_key(ctx: GenDeriveContext) -> bytes:
    """The column's 32-byte selection key (a valid `mask_key` for the index kernel)."""
    return ctx.family_bytes(SELECT_FAMILY)


def uniform_indexer(ctx: GenDeriveContext, n: int) -> PositionalIndexer | None:
    """Uniform selection over `n` choices, or `None` when `n` is empty or beyond the kernel's range."""
    if n < 1 or n > MAX_POOL:
        return None
    return PositionalIndexer(selection_key(ctx), UNIFORM_NAMESPACE, n)


def cdf_thresholds(weights: Sequence[Any], n_choices: int) -> list[int] | None:
    """Cumulative integer thresholds over `CDF_RESOLUTION`, or `None` to decline the weights.

    Entry `i` is the exclusive upper bound of choice `i`'s slice, so a right bisect of a uniform
    integer `u` finds the choice. A zero-weight choice owns an empty slice and is never drawn,
    which matches `random.choices`. Weights this resolution cannot honour decline instead of
    rounding a real choice out of existence.
    """
    if len(weights) != n_choices or n_choices < 1:
        return None
    try:
        values = [float(w) for w in weights]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and v >= 0 for v in values):
        return None
    total = math.fsum(values)
    if not math.isfinite(total) or total <= 0:
        return None
    thresholds: list[int] = []
    running = 0.0
    previous = 0
    for v in values:
        running += v
        threshold = int(running / total * CDF_RESOLUTION)
        if v > 0 and threshold == previous:
            return None
        thresholds.append(threshold)
        previous = threshold
    # Absorb float drift so the last choice owns the end of the draw space.
    thresholds[-1] = CDF_RESOLUTION
    return thresholds


@dataclass(frozen=True)
class WeightedIndexer:
    """Inverse-transform selection: an HMAC-derived uniform integer looked up in a CDF."""

    base: PositionalIndexer
    thresholds: np.ndarray[Any, Any]

    def draw(self, offset: int, size: int) -> np.ndarray[Any, Any]:
        u = self.base.draw(offset, size)
        # Right bisect: choice i owns [T[i-1], T[i]). int64 holds every u < 2**32 exactly.
        return np.searchsorted(self.thresholds, u.astype(np.int64), side="right")


Indexer = PositionalIndexer | WeightedIndexer


def weighted_indexer(
    ctx: GenDeriveContext, weights: Sequence[Any], n_choices: int
) -> WeightedIndexer | None:
    """Weighted selection over `n_choices`, or `None` when the weights must stay on Python."""
    thresholds = cdf_thresholds(weights, n_choices)
    if thresholds is None:
        return None
    base = PositionalIndexer(selection_key(ctx), WEIGHTED_NAMESPACE, CDF_RESOLUTION)
    return WeightedIndexer(base, np.asarray(thresholds, dtype=np.int64))


def choice_indexer(
    ctx: GenDeriveContext, n_choices: int, weights: Sequence[Any] | None
) -> Indexer | None:
    """The kernel indexer for a categorical-style choice, or `None` to keep the Python RNG.

    No weights means uniform. Configured weights stay weighted: they are mapped through the CDF
    or declined, never replaced by a uniform draw.
    """
    if weights is None:
        return uniform_indexer(ctx, n_choices)
    return weighted_indexer(ctx, weights, n_choices)


__all__ = [
    "CDF_RESOLUTION",
    "MAX_POOL",
    "SELECT_FAMILY",
    "UNIFORM_NAMESPACE",
    "WEIGHTED_NAMESPACE",
    "Indexer",
    "PositionalIndexer",
    "WeightedIndexer",
    "cdf_thresholds",
    "choice_indexer",
    "selection_key",
    "uniform_indexer",
    "weighted_indexer",
]
