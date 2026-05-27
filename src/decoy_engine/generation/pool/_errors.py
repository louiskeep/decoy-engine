"""Exception hierarchy for the pool package.

`GenerationError` covers value-generation failures at sample time
(canonicalization, dtype mismatches, uniqueness impossibility).
`PoolCapacityError` covers pool-construction + cache-budget failures.
Both are runtime errors; peers, not subclasses of `PlanCompileError`.
"""

from __future__ import annotations


class GenerationError(Exception):
    """Runtime failure inside the pool sampler or canonicalizer.

    Codes used in S5:

    - `deterministic_requires_source_and_namespace`: PoolSampler.sample called
      with `deterministic=True` but `source` or `namespace` missing.
    - `uniqueness_impossible`: UNIQUE-mode sample request larger than pool.
    - `dtype_unsafe`: output dtype is not safely convertible to source dtype.
    - `float_canonicalization_unsupported`: float source value at canonicalize
      time (the encoding would lock into the determinism envelope per R3).
    - `timezone_naive_datetime`: datetime source value without an explicit tz.
    - `null_canonicalization_unreachable`: null reached `_canonicalize_source`
      (nulls should be filtered at the mask layer before canonicalize).
    """

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


class PoolCapacityError(Exception):
    """Pool-construction or cache-budget failure.

    Codes used in S5:

    - `provider_not_poolable`: PoolBuilder.build called for a provider whose
      CapabilityMatrix declares `poolable: False`.
    - `pool_exceeds_cache_budget`: PoolCache.put called with a pool larger
      than the entire cache budget (eviction cannot make room).
    - `pool_too_small_for_source`: pool_capacity_pre_flight detected a UNIQUE
      mode column whose source distinct count exceeds the configured pool size
      AND on_pool_exhaustion=fail.
    """

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")
