"""composite_provider: coherent (npi, provider_name, practice_address) (engine-v2 MG-4).

3-output composite for healthcare provider records. Builds on the
MG-1 S4 npi domain generator (shipped 2026-06-01 via PanAdapter /
Icd10Adapter / IbanAdapter / CusipAdapter / NpiAdapter).

Outputs:
- npi: 10-digit NPI with CMS Luhn check digit, from the
  synthetic_npi domain adapter. Validator-passing by construction.
- provider_name: faker.name pool draw.
- practice_address: flat-string locality concatenation
  ("<city>, <state> <zip>") drawn from the same US locality table
  that composite_city_state_zip uses (membership-by-construction).
  Flat string per spec time vs nested object choice.

Shared-latent design: ONE `derive(seed, coherent_namespace, source)`
call per row returns 32 bytes; sliced into 3 x 8-byte ranges for the
3 outputs.

Methodology: composite_name_email shared-latent pattern; npi
derivation delegated to NpiDomain.from_bytes (MG-1 S4).
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


def _practice_address(city: str, state: str, zip_: str) -> str:
    return f"{city}, {state} {zip_}"


class CompositeProvider:
    """CompositeGenerator for coherent (npi/provider_name/practice_address) bundles."""

    composite_name: str = "composite_provider"
    # Sorted form so the wiring check matches the sorted coherent group.
    output_columns: tuple[str, ...] = ("npi", "practice_address", "provider_name")

    def __init__(
        self,
        *,
        coherent_namespace: str,
        registry: "ProviderRegistry | None" = None,
        pool_size: int = 10_000,
    ) -> None:
        self.coherent_namespace = coherent_namespace
        self._registry = registry
        self._pool_size = pool_size
        self._names: np.ndarray | None = None
        self._locality = load_locality_table()

    def _name_pool(self, spec: ProviderSpec) -> np.ndarray:
        if self._names is None:
            from decoy_engine.generation.pool._builder import PoolBuilder
            from decoy_engine.providers_v2 import get_default_registry

            registry = self._registry if self._registry is not None else get_default_registry()
            builder = PoolBuilder(registry)
            seed = spec.seed if spec.seed is not None else b"\x00" * 8
            locale = spec.locale or "en_US"
            pool = builder.build(
                provider="person_name",
                size=self._pool_size,
                job_seed=seed,
                locale=locale,
                config={},
                namespace=self.coherent_namespace,
            )
            self._names = pool.values
        return self._names

    def _check_pool_size(self, size: int) -> None:
        if size > _POOL_SIZE_MAX:
            raise CompositeError(
                code="pool_size_overflow",
                message=(
                    f"pool size {size} exceeds {_POOL_SIZE_MAX}; the sub-byte "
                    "modulo-bias bound requires <= 2**56."
                ),
            )

    @staticmethod
    def _npi_from_bytes(b: bytes) -> str:
        """Generate an NPI body + CMS Luhn check digit deterministically
        from 8 bytes. Delegates to the MG-1 S4 NpiDomain pattern so the
        result is validator-passing by construction."""
        from decoy_engine.providers_v2.identifiers._npi import NpiDomain

        # NpiDomain.from_bytes wants 32 bytes; left-pad the 8-byte slice
        # with zeros so the body math stays deterministic but uses our
        # 8-byte slot.
        return NpiDomain().from_bytes(b + b"\x00" * 24)

    def build_pool(self, spec: ProviderSpec, *, size: int | None = None) -> BundlePool:
        n = size if size is not None else self._pool_size
        names = self._name_pool(spec)
        seed = spec.seed if spec.seed is not None else b"\x00" * 8
        rng = np.random.default_rng(int.from_bytes(seed, "big"))
        ni = rng.integers(0, len(names), size=n)
        li = rng.integers(0, len(self._locality), size=n)
        # NPI draw: 8 bytes per row from the seed-derived RNG. We use
        # the rng to keep the build-time path stable, deriving NPI bytes
        # via rng.bytes for each row.
        values = np.empty(n, dtype=object)
        for k in range(n):
            npi_bytes = rng.bytes(8)
            city, state, zip_ = self._locality[li[k]]
            values[k] = (
                self._npi_from_bytes(npi_bytes),
                _practice_address(city, state, zip_),
                names[ni[k]],
            )
        values.setflags(write=False)
        return BundlePool(
            values=values,
            provider=self.composite_name,
            locale=spec.locale or "en_US",
            config_hash="provider/v1",
            seed=seed,
            size=n,
            build_time_ms=0.0,
            backend_type="composite",
            backend_version="provider/v1",
            distinct_count=n,
            output_columns=self.output_columns,
        )

    def _generate_deterministic(
        self, spec: ProviderSpec, source: pd.Series
    ) -> dict[str, pd.Series]:
        assert spec.seed is not None  # noqa: S101 -- guarded by generate_bundle
        names = self._name_pool(spec)
        self._check_pool_size(len(names))
        self._check_pool_size(len(self._locality))

        npi_vals: list[Any] = []
        name_vals: list[Any] = []
        addr_vals: list[Any] = []
        is_null = source.isna()
        for i in range(len(source)):
            if is_null.iloc[i]:
                npi_vals.append(pd.NA)
                name_vals.append(pd.NA)
                addr_vals.append(pd.NA)
                continue
            canonical = _canonicalize_source(source.iloc[i])
            latent = derive(spec.seed, self.coherent_namespace, canonical)
            npi_bytes = latent[0:8]
            name_idx = int.from_bytes(latent[8:16], "big") % len(names)
            locality_idx = int.from_bytes(latent[16:24], "big") % len(self._locality)
            npi_vals.append(self._npi_from_bytes(npi_bytes))
            name_vals.append(names[name_idx])
            city, state, zip_ = self._locality[locality_idx]
            addr_vals.append(_practice_address(city, state, zip_))
        return {
            "npi": pd.Series(npi_vals),
            "provider_name": pd.Series(name_vals),
            "practice_address": pd.Series(addr_vals),
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
                    message="Deterministic composite_provider requires source + seed.",
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


def composite_provider(
    *,
    coherent_namespace: str,
    registry: "ProviderRegistry | None" = None,
    pool_size: int = 10_000,
) -> CompositeProvider:
    return CompositeProvider(
        coherent_namespace=coherent_namespace,
        registry=registry,
        pool_size=pool_size,
    )


__all__: list[str] = ["CompositeProvider", "composite_provider"]
