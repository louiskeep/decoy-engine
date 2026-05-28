"""composite_name_email: coherent (first_name, last_name, email) (engine-v2 S8).

The shared-latent design (S8 spec §4, the hard part): for each source row a
SINGLE `derive(seed, coherent_namespace, _canonicalize_source(source))` call
produces 32 bytes, sub-byte-sliced into three 8-byte ranges that index the
first-name / last-name / domain pools. One draw keeps the three sub-fields
coupled and re-run-stable; three independent `derive` calls would let the names
drift apart across runs and break the bundle identity. `derive_index` is NOT
reused (it consumes only bytes[0:8]); this is a USE of `derive(...)`, not an
envelope extension (R3). 24 of 32 bytes are used; a fourth sub-field fits, a
fifth needs a second `derive` keyed on `coherent_namespace + "/ext"`.

Canonicalization MUST go through `_canonicalize_source` (R3) so a deterministic
composite column and a deterministic scalar column keyed on the same int/date
source produce identical mappings.

email default = `<first>.<last>@<domain>` (RESOLVED PO Session 33; matches the
golden fixture). Override via `spec.extra["email_format"]`. Domain pool from
`spec.extra["domain_pool"]` or a default; an explicitly empty list raises.

Methodology: SDV's HMA1 shared-latent pattern for correlated columns
(best-practices §6.2).
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

_DEFAULT_DOMAINS: tuple[str, ...] = (
    "example.com",
    "example.org",
    "example.net",
    "mail.example.com",
    "corp.example.com",
)
_DEFAULT_EMAIL_FORMAT = "{first}.{last}@{domain}"
_POOL_SIZE_MAX = 2**56  # match derive_index's pool_size_overflow ceiling (R3 bias bound)


class CompositeNameEmail:
    """CompositeGenerator for coherent first/last/email bundles."""

    composite_name: str = "composite_name_email"
    output_columns: tuple[str, ...] = ("first_name", "last_name", "email")

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
        self._first: np.ndarray[Any, Any] | None = None
        self._last: np.ndarray[Any, Any] | None = None

    def _name_pools(self, spec: ProviderSpec) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]:
        """Build (and cache) the first/last scalar pools, reproducibly from the seed."""
        if self._first is None or self._last is None:
            from decoy_engine.generation.pool._builder import PoolBuilder
            from decoy_engine.providers_v2 import get_default_registry

            registry = self._registry if self._registry is not None else get_default_registry()
            builder = PoolBuilder(registry)
            seed = spec.seed if spec.seed is not None else b"\x00" * 8
            locale = spec.locale or "en_US"
            first_pool = builder.build(
                provider="person_first_name",
                size=self._pool_size,
                job_seed=seed,
                locale=locale,
                config={},
                namespace=self.coherent_namespace,
            )
            last_pool = builder.build(
                provider="person_last_name",
                size=self._pool_size,
                job_seed=seed,
                locale=locale,
                config={},
                namespace=self.coherent_namespace,
            )
            self._first = first_pool.values
            self._last = last_pool.values
        return self._first, self._last

    def _resolve_domains(self, spec: ProviderSpec) -> tuple[str, ...]:
        configured = spec.extra.get("domain_pool")
        if configured is None:
            return _DEFAULT_DOMAINS
        if len(configured) == 0:
            raise CompositeError(
                code="empty_domain_pool",
                message="composite_name_email got an explicitly empty domain_pool ([]).",
            )
        return tuple(configured)

    @staticmethod
    def _email(first: Any, last: Any, domain: str, fmt: str) -> str:
        return fmt.format(first=str(first).lower(), last=str(last).lower(), domain=domain)

    def _check_pool_size(self, size: int) -> None:
        if size > _POOL_SIZE_MAX:
            raise CompositeError(
                code="pool_size_overflow",
                message=(
                    f"name/last/domain pool size {size} exceeds {_POOL_SIZE_MAX}; the "
                    "sub-byte modulo-bias bound (matching derive_index) requires <= 2**56."
                ),
            )

    def build_pool(self, spec: ProviderSpec, *, size: int | None = None) -> BundlePool:
        """Build a BundlePool of coherent (first, last, email) tuples (non-det path)."""
        n = size if size is not None else self._pool_size
        first_arr, last_arr = self._name_pools(spec)
        domains = self._resolve_domains(spec)
        fmt = str(spec.extra.get("email_format", _DEFAULT_EMAIL_FORMAT))
        seed = spec.seed if spec.seed is not None else b"\x00" * 8
        rng = np.random.default_rng(int.from_bytes(seed, "big"))
        fi = rng.integers(0, len(first_arr), size=n)
        li = rng.integers(0, len(last_arr), size=n)
        di = rng.integers(0, len(domains), size=n)
        values = np.empty(n, dtype=object)
        for k in range(n):
            first, last, domain = first_arr[fi[k]], last_arr[li[k]], domains[di[k]]
            values[k] = (first, last, self._email(first, last, domain, fmt))
        values.setflags(write=False)
        return BundlePool(
            values=values,
            provider=self.composite_name,
            locale=spec.locale or "en_US",
            config_hash="name_email/v1",
            seed=seed,
            size=n,
            build_time_ms=0.0,
            backend_type="composite",
            backend_version="name_email/v1",
            distinct_count=n,
            output_columns=self.output_columns,
        )

    def _generate_deterministic(
        self, spec: ProviderSpec, source: pd.Series
    ) -> dict[str, pd.Series]:
        assert spec.seed is not None  # noqa: S101 -- guarded by generate_bundle
        first_arr, last_arr = self._name_pools(spec)
        domains = self._resolve_domains(spec)
        fmt = str(spec.extra.get("email_format", _DEFAULT_EMAIL_FORMAT))
        fp_size, lp_size, dp_size = len(first_arr), len(last_arr), len(domains)
        for pool_len in (fp_size, lp_size, dp_size):
            self._check_pool_size(pool_len)

        first_vals: list[Any] = []
        last_vals: list[Any] = []
        email_vals: list[Any] = []
        is_null = source.isna()
        for i in range(len(source)):
            if is_null.iloc[i]:
                first_vals.append(pd.NA)
                last_vals.append(pd.NA)
                email_vals.append(pd.NA)
                continue
            canonical = _canonicalize_source(source.iloc[i])
            latent = derive(spec.seed, self.coherent_namespace, canonical)
            first_idx = int.from_bytes(latent[0:8], "big") % fp_size
            last_idx = int.from_bytes(latent[8:16], "big") % lp_size
            domain_idx = int.from_bytes(latent[16:24], "big") % dp_size
            first = first_arr[first_idx]
            last = last_arr[last_idx]
            domain = domains[domain_idx]
            first_vals.append(first)
            last_vals.append(last)
            email_vals.append(self._email(first, last, domain, fmt))
        return {
            "first_name": pd.Series(first_vals),
            "last_name": pd.Series(last_vals),
            "email": pd.Series(email_vals),
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
                    message="Deterministic composite_name_email requires source + seed.",
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


def composite_name_email(
    *,
    coherent_namespace: str,
    registry: ProviderRegistry | None = None,
    pool_size: int = 10_000,
) -> CompositeNameEmail:
    """Construct a CompositeNameEmail generator bound to `coherent_namespace`."""
    return CompositeNameEmail(
        coherent_namespace=coherent_namespace, registry=registry, pool_size=pool_size
    )


__all__: list[str] = ["CompositeNameEmail", "composite_name_email"]
