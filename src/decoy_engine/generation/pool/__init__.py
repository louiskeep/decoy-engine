"""Pool Manager: pool-backed sampling primitives for V2 strategies.

Public API:

    from decoy_engine.generation.pool import (
        ValuePool,
        PoolBuilder,
        PoolCache,
        PoolSampler,
        PoolAdapter,
        CardinalityMode,
        GenerationError,
        PoolCapacityError,
        QualityWarning,
        get_default_pool_cache,
    )

Ten public names; all in `__all__`. PoolAdapter is the first concrete
`BackendAdapter` (S4 Protocol) that declares
`supports_deterministic: True` for the poolable subset of the catalog;
it wraps another adapter (typically FakerAdapter) and routes
deterministic calls through a pool indexed by `derive_index`.

Source patterns:
- Bulk-fill: SDV `SingleTablePreset` build-and-sample; Faker batch usage.
- Bytes-bounded LRU: pandas `io.parquet.row_group_size`, Polars
  `set_global_string_cache` budget pattern (per best-practices §6.2).
- NEP-19 (`numpy.random.default_rng`): the seed-stability contract.
- Adapter wrapping: V1 strategy decorators; SDV's `SingleTablePreset`
  wrapping `GaussianCopulaSynthesizer`.

References:
- Spec: docs/v2/sprints/engine-v2/sprint-05-pool-manager.md
- Cross-sprint contracts §2.4 (cardinality mode), §2.5 (pool identity),
  §2.6 (sampler), §4 row 7 (pool_capacity_pre_flight), R3 (canonicalize
  envelope), R6 (R6 reshape).
"""

from __future__ import annotations

from decoy_engine.generation.pool._builder import PoolBuilder
from decoy_engine.generation.pool._cache import (
    CacheStats,
    PoolCache,
    get_default_pool_cache,
)
from decoy_engine.generation.pool._cardinality import CardinalityMode
from decoy_engine.generation.pool._errors import GenerationError, PoolCapacityError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.generation.pool._pool_adapter import PoolAdapter
from decoy_engine.generation.pool._sampler import PoolSampler
from decoy_engine.generation.pool._validate import check_pool_capacity_pre_flight
from decoy_engine.generation.pool._value_pool import ValuePool

__all__ = [
    "CacheStats",
    "CardinalityMode",
    "GenerationError",
    "PoolAdapter",
    "PoolBuilder",
    "PoolCache",
    "PoolCapacityError",
    "PoolSampler",
    "QualityWarning",
    "ValuePool",
    "check_pool_capacity_pre_flight",
    "get_default_pool_cache",
]
