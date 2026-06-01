"""composite_custom: arbitrary 1-4 column coherent bundle (engine-v2 MG-4).

Declared in YAML as:

    tables:
      t:
        columns:
          - name: a
            strategy: faker
            provider: composite_custom
            coherent_with: [b, c]
            provider_config:
              bundle:
                - {column: a, provider: person_first_name}
                - {column: b, provider: person_last_name}
                - {column: c, provider: person_email}
          - name: b
            strategy: faker
            provider: composite_custom
            coherent_with: [a, c]
            provider_config:
              bundle:
                - {column: a, provider: person_first_name}
                - {column: b, provider: person_last_name}
                - {column: c, provider: person_email}
          - name: c
            strategy: faker
            provider: composite_custom
            coherent_with: [a, b]
            provider_config:
              bundle: ...

The coherent group is the SET (a, b, c). For each source row a SINGLE
`derive(seed, coherent_namespace, _canonicalize_source(source))` call
returns 32 bytes. The bytes are sub-byte-sliced into 8-byte ranges
that index each slot's pool. ONE draw per row keeps the slots
coupled and re-run-stable.

Constraints:
- Bundle size: 1 to 4 outputs. The 32-byte derive ceiling caps the
  bundle at 4 outputs at 8 bytes each; a fifth would require a
  second `derive` call keyed on `coherent_namespace + "/ext"`.
  Operators needing more columns declare a second composite_custom
  block.
- No nested composites: a bundle item's `provider` must NOT begin
  with `composite_`. Locked at construction time + at plan-compile.
- Statistical independence within a row: the coherence is identity
  stability (same source -> same triple), NOT statistical
  (generating "Alice" does NOT bias toward "alice@email"). For
  shape-coherent outputs, use `composite_person`.

Methodology: mirrors the shared-latent pattern from
composite_name_email (SDV's HMA1-style per the engine-v2 S8 spec).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.generation.composite._bundle_pool import BundlePool
from decoy_engine.generation.composite._errors import CompositeError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.providers_v2._adapter import ProviderSpec

if TYPE_CHECKING:
    from decoy_engine.providers_v2 import ProviderRegistry


_MAX_BUNDLE_SIZE = 4  # 32 bytes / 8 bytes per slot.
_MIN_BUNDLE_SIZE = 1
_POOL_SIZE_MAX = 2**56  # match derive_index's pool_size_overflow ceiling.


class CompositeCustom:
    """CompositeGenerator for arbitrary 1-4 column coherent bundles.

    The `bundle` config carries `[{column, provider, config?}]` items.
    `output_columns` is the tuple of columns from the bundle, sorted
    so the wiring check at `_validate.py` matches the coherent group.
    """

    composite_name: str = "composite_custom"

    def __init__(
        self,
        *,
        coherent_namespace: str,
        bundle: list[dict[str, Any]],
        registry: "ProviderRegistry | None" = None,
        pool_size: int = 10_000,
    ) -> None:
        if not _MIN_BUNDLE_SIZE <= len(bundle) <= _MAX_BUNDLE_SIZE:
            raise CompositeError(
                code="composite_custom_bundle_size",
                message=(
                    f"composite_custom bundle must hold {_MIN_BUNDLE_SIZE}-"
                    f"{_MAX_BUNDLE_SIZE} outputs; got {len(bundle)}. "
                    "Use multiple composite_custom blocks for larger groups."
                ),
            )
        for item in bundle:
            if not isinstance(item, dict):
                raise CompositeError(
                    code="composite_custom_bundle_item_shape",
                    message=f"bundle item is not a dict: {item!r}",
                )
            if "column" not in item or "provider" not in item:
                raise CompositeError(
                    code="composite_custom_bundle_item_missing_keys",
                    message=(
                        f"each bundle item requires column + provider; got "
                        f"{sorted(item.keys())!r}"
                    ),
                )
            if isinstance(item["provider"], str) and item["provider"].startswith("composite_"):
                raise CompositeError(
                    code="composite_custom_no_nesting",
                    message=(
                        f"bundle item references composite provider "
                        f"{item['provider']!r}; nested composites are not "
                        "supported in V1."
                    ),
                )
        self.coherent_namespace = coherent_namespace
        self.bundle = bundle
        # Output columns are sorted so they match the wiring-check
        # contract (sorted coherent group).
        self.output_columns: tuple[str, ...] = tuple(
            sorted(item["column"] for item in bundle)
        )
        self._registry = registry
        self._pool_size = pool_size
        self._pools: list[np.ndarray] | None = None
        # Per-slot column lookup for the writeback step.
        self._slot_columns: tuple[str, ...] = tuple(item["column"] for item in bundle)

    def _check_pool_size(self, size: int) -> None:
        if size > _POOL_SIZE_MAX:
            raise CompositeError(
                code="pool_size_overflow",
                message=(
                    f"bundle pool size {size} exceeds {_POOL_SIZE_MAX}; the "
                    "sub-byte modulo-bias bound (matching derive_index) "
                    "requires <= 2**56."
                ),
            )

    def _build_pools(self, spec: ProviderSpec) -> list[np.ndarray]:
        """Build per-slot pools, reproducibly from the seed.

        One pool per bundle item, each drawn from its declared provider.
        Pool draws are keyed by `coherent_namespace#<slot>` so two
        bundles with the same providers in different orders produce
        different pools (correct: ordering is significant for the
        sub-byte slicing).
        """
        if self._pools is not None:
            return self._pools

        from decoy_engine.generation.pool._builder import PoolBuilder
        from decoy_engine.providers_v2 import get_default_registry

        registry = self._registry if self._registry is not None else get_default_registry()
        builder = PoolBuilder(registry)
        seed = spec.seed if spec.seed is not None else b"\x00" * 8
        locale = spec.locale or "en_US"
        pools: list[np.ndarray] = []
        for slot, item in enumerate(self.bundle):
            slot_ns = f"{self.coherent_namespace}#{slot}"
            pool = builder.build(
                provider=item["provider"],
                size=self._pool_size,
                job_seed=seed,
                locale=locale,
                config=item.get("config") or {},
                namespace=slot_ns,
            )
            pools.append(pool.values)
            self._check_pool_size(len(pool.values))
        self._pools = pools
        return pools

    def build_pool(self, spec: ProviderSpec, *, size: int | None = None) -> BundlePool:
        """Build a BundlePool of coherent bundle tuples (non-det path)."""
        n = size if size is not None else self._pool_size
        pools = self._build_pools(spec)
        seed = spec.seed if spec.seed is not None else b"\x00" * 8
        rng = np.random.default_rng(int.from_bytes(seed, "big"))
        # One uniform draw per slot to form n coherent tuples.
        slot_indices = [rng.integers(0, len(pools[s]), size=n) for s in range(len(pools))]
        values = np.empty(n, dtype=object)
        for k in range(n):
            tup = tuple(pools[s][slot_indices[s][k]] for s in range(len(pools)))
            values[k] = tup
        values.setflags(write=False)
        return BundlePool(
            values=values,
            provider=self.composite_name,
            locale=spec.locale or "en_US",
            config_hash="custom/v1",
            seed=seed,
            size=n,
            build_time_ms=0.0,
            backend_type="composite",
            backend_version="custom/v1",
            distinct_count=n,
            output_columns=self.output_columns,
        )

    def _generate_deterministic(
        self, spec: ProviderSpec, source: pd.Series
    ) -> dict[str, pd.Series]:
        assert spec.seed is not None  # noqa: S101 -- guarded by generate_bundle
        pools = self._build_pools(spec)
        n_outputs = len(self.bundle)

        per_slot_values: list[list[Any]] = [[] for _ in range(n_outputs)]
        is_null = source.isna()
        for i in range(len(source)):
            if is_null.iloc[i]:
                for s in range(n_outputs):
                    per_slot_values[s].append(pd.NA)
                continue
            canonical = _canonicalize_source(source.iloc[i])
            latent = derive(spec.seed, self.coherent_namespace, canonical)
            for s in range(n_outputs):
                chunk = latent[s * 8 : (s + 1) * 8]
                idx = int.from_bytes(chunk, "big") % len(pools[s])
                per_slot_values[s].append(pools[s][idx])

        return {
            self._slot_columns[s]: pd.Series(per_slot_values[s])
            for s in range(n_outputs)
        }

    def generate_bundle(
        self,
        spec: ProviderSpec,
        count: int,
        *,
        source: pd.Series | None = None,
        deterministic: bool = False,
    ) -> dict[str, pd.Series]:
        if deterministic:
            if source is None or spec.seed is None:
                raise CompositeError(
                    code="deterministic_requires_source_and_seed",
                    message=(
                        "Deterministic composite_custom requires source + seed."
                    ),
                )
            if len(source) != count:
                raise CompositeError(
                    code="source_length_mismatch",
                    message=f"source length {len(source)} != count {count}.",
                )
            return self._generate_deterministic(spec, source)
        # Non-deterministic: draw coherent bundles from the pool.
        from decoy_engine.generation.pool._cardinality import CardinalityMode
        from decoy_engine.generation.pool._sampler import PoolSampler

        pool = self.build_pool(spec)
        seed = spec.seed if spec.seed is not None else os.urandom(8)
        return PoolSampler().sample_bundle(
            pool, count, mode=CardinalityMode.REUSE, seed=seed, deterministic=False
        )


def composite_custom(
    *,
    coherent_namespace: str,
    bundle: list[dict[str, Any]],
    registry: "ProviderRegistry | None" = None,
    pool_size: int = 10_000,
) -> CompositeCustom:
    """Construct a CompositeCustom generator bound to `coherent_namespace`."""
    return CompositeCustom(
        coherent_namespace=coherent_namespace,
        bundle=bundle,
        registry=registry,
        pool_size=pool_size,
    )


__all__: list[str] = ["CompositeCustom", "composite_custom"]
