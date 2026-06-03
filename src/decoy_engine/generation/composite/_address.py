"""composite_address: coherent (street, city, state, zip) (engine-v2 MG-4).

Extends composite_city_state_zip with a street_address output. The
city/state/zip triple stays membership-by-construction (verbatim from
the locality table) so the bundle never produces a Texas city with
a Chicago ZIP. Street is drawn from faker's street_address pool
independently; its independence is intentional (a random street
number + name is operationally fine, and tying street to locality
would require a much larger reference table).

Shared-latent design: ONE `derive(seed, coherent_namespace, source)`
call per row returns 32 bytes; sliced into 2 x 8-byte ranges that
index the street pool + the locality table. The 16 unused bytes are
reserved for future per-bundle extensions.

Methodology: identical pattern to composite_city_state_zip
(`_city_state_zip.py`) with one extra independent slot for street.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.generation.composite._bundle_pool import BundlePool
from decoy_engine.generation.composite._city_state_zip import load_locality_table
from decoy_engine.generation.composite._errors import CompositeError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.providers_v2._adapter import ProviderSpec

if TYPE_CHECKING:
    from decoy_engine.providers_v2 import ProviderRegistry


_POOL_SIZE_MAX = 2**56


class CompositeAddress:
    """CompositeGenerator for coherent (street/city/state/zip) bundles."""

    composite_name: str = "composite_address"
    # Sorted form so the wiring check matches the sorted coherent group.
    output_columns: tuple[str, ...] = ("city", "state", "street_address", "zip")

    def __init__(
        self,
        *,
        coherent_namespace: str,
        registry: ProviderRegistry | None = None,
        pool_size: int = 10_000,
    ) -> None:
        self.coherent_namespace = coherent_namespace
        self._registry = registry
        self._pool_size = pool_size
        self._street: np.ndarray | None = None
        self._locality = load_locality_table()

    def _street_pool(self, spec: ProviderSpec) -> np.ndarray:
        if self._street is None:
            from decoy_engine.generation.pool._builder import PoolBuilder
            from decoy_engine.providers_v2 import get_default_registry

            registry = self._registry if self._registry is not None else get_default_registry()
            builder = PoolBuilder(registry)
            seed = spec.seed if spec.seed is not None else b"\x00" * 8
            locale = spec.locale or "en_US"
            pool = builder.build(
                provider="address_street",
                size=self._pool_size,
                job_seed=seed,
                locale=locale,
                config={},
                namespace=self.coherent_namespace,
            )
            self._street = pool.values
        return self._street

    def _check_pool_size(self, size: int) -> None:
        if size > _POOL_SIZE_MAX:
            raise CompositeError(
                code="pool_size_overflow",
                message=(
                    f"pool size {size} exceeds {_POOL_SIZE_MAX}; the sub-byte "
                    "modulo-bias bound requires <= 2**56."
                ),
            )

    def build_pool(self, spec: ProviderSpec, *, size: int | None = None) -> BundlePool:
        n = size if size is not None else self._pool_size
        streets = self._street_pool(spec)
        seed = spec.seed if spec.seed is not None else b"\x00" * 8
        rng = np.random.default_rng(int.from_bytes(seed, "big"))
        si = rng.integers(0, len(streets), size=n)
        li = rng.integers(0, len(self._locality), size=n)
        values = np.empty(n, dtype=object)
        for k in range(n):
            city, state, zip_ = self._locality[li[k]]
            street = streets[si[k]]
            # Order matches output_columns (alphabetical): city, state, street_address, zip.
            values[k] = (city, state, street, zip_)
        values.setflags(write=False)
        return BundlePool(
            values=values,
            provider=self.composite_name,
            locale=spec.locale or "en_US",
            config_hash="address/v1",
            seed=seed,
            size=n,
            build_time_ms=0.0,
            backend_type="composite",
            backend_version="address/v1",
            distinct_count=n,
            output_columns=self.output_columns,
        )

    def _generate_deterministic(
        self, spec: ProviderSpec, source: pd.Series
    ) -> dict[str, pd.Series]:
        assert spec.seed is not None  # noqa: S101 -- guarded by generate_bundle
        streets = self._street_pool(spec)
        self._check_pool_size(len(streets))
        self._check_pool_size(len(self._locality))

        street_vals: list[Any] = []
        city_vals: list[Any] = []
        state_vals: list[Any] = []
        zip_vals: list[Any] = []
        is_null = source.isna()
        for i in range(len(source)):
            if is_null.iloc[i]:
                street_vals.append(pd.NA)
                city_vals.append(pd.NA)
                state_vals.append(pd.NA)
                zip_vals.append(pd.NA)
                continue
            canonical = _canonicalize_source(source.iloc[i])
            latent = derive(spec.seed, self.coherent_namespace, canonical)
            street_idx = int.from_bytes(latent[0:8], "big") % len(streets)
            locality_idx = int.from_bytes(latent[8:16], "big") % len(self._locality)
            street_vals.append(streets[street_idx])
            city, state, zip_ = self._locality[locality_idx]
            city_vals.append(city)
            state_vals.append(state)
            zip_vals.append(zip_)
        return {
            "street_address": pd.Series(street_vals),
            "city": pd.Series(city_vals),
            "state": pd.Series(state_vals),
            "zip": pd.Series(zip_vals),
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
                    message="Deterministic composite_address requires source + seed.",
                )
            if len(source) != count:
                raise CompositeError(
                    code="source_length_mismatch",
                    message=f"source length {len(source)} != count {count}.",
                )
            return self._generate_deterministic(spec, source)
        from decoy_engine.generation.pool._cardinality import CardinalityMode
        from decoy_engine.generation.pool._sampler import PoolSampler

        pool = self.build_pool(spec)
        seed = spec.seed if spec.seed is not None else os.urandom(8)
        return PoolSampler().sample_bundle(
            pool, count, mode=CardinalityMode.REUSE, seed=seed, deterministic=False
        )


def composite_address(
    *,
    coherent_namespace: str,
    registry: ProviderRegistry | None = None,
    pool_size: int = 10_000,
) -> CompositeAddress:
    return CompositeAddress(
        coherent_namespace=coherent_namespace,
        registry=registry,
        pool_size=pool_size,
    )


__all__: list[str] = ["CompositeAddress", "composite_address"]
