"""composite_person: coherent (first_name, last_name, email, dob) (engine-v2 MG-4).

Extends composite_name_email with a date_of_birth output drawn from
faker's date_of_birth pool. The email field stays shape-coherent
("{first}.{last}@{domain}") so the bundle is still identity-stable
(same source -> same 4-tuple).

Shared-latent design: ONE `derive(seed, coherent_namespace, source)`
call per row returns 32 bytes; sliced into 4 x 8-byte ranges that
index the first / last / domain / dob pools. The email is built
from the picked first + last + domain, not from an independent pool
slice.

Methodology: identical pattern to composite_name_email
(`_name_email.py`), with one extra output slot (dob) and the email
derivation reused unchanged. Mirrors SDV's HMA1 shared-latent
approach per engineering-best-practices section 6.2.
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
_POOL_SIZE_MAX = 2**56


class CompositePerson:
    """CompositeGenerator for coherent (first/last/email/dob) bundles."""

    composite_name: str = "composite_person"
    # Sorted form so the wiring check matches the sorted coherent group.
    output_columns: tuple[str, ...] = ("dob", "email", "first_name", "last_name")

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
        self._first: np.ndarray | None = None
        self._last: np.ndarray | None = None
        self._dob: np.ndarray | None = None

    def _pools(
        self, spec: ProviderSpec
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Build (and cache) first/last/dob pools, reproducibly from the seed."""
        if self._first is None or self._last is None or self._dob is None:
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
            dob_pool = builder.build(
                provider="person_dob",
                size=self._pool_size,
                job_seed=seed,
                locale=locale,
                config={},
                namespace=self.coherent_namespace,
            )
            self._first = first_pool.values
            self._last = last_pool.values
            self._dob = dob_pool.values
        return self._first, self._last, self._dob

    def _resolve_domains(self, spec: ProviderSpec) -> tuple[str, ...]:
        configured = spec.extra.get("domain_pool")
        if configured is None:
            return _DEFAULT_DOMAINS
        if len(configured) == 0:
            raise CompositeError(
                code="empty_domain_pool",
                message="composite_person got an explicitly empty domain_pool ([]).",
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
                    f"pool size {size} exceeds {_POOL_SIZE_MAX}; the sub-byte "
                    "modulo-bias bound requires <= 2**56."
                ),
            )

    def build_pool(self, spec: ProviderSpec, *, size: int | None = None) -> BundlePool:
        """Build a BundlePool of coherent (first, last, email, dob) tuples."""
        n = size if size is not None else self._pool_size
        first_arr, last_arr, dob_arr = self._pools(spec)
        domains = self._resolve_domains(spec)
        fmt = str(spec.extra.get("email_format", _DEFAULT_EMAIL_FORMAT))
        seed = spec.seed if spec.seed is not None else b"\x00" * 8
        rng = np.random.default_rng(int.from_bytes(seed, "big"))
        fi = rng.integers(0, len(first_arr), size=n)
        li = rng.integers(0, len(last_arr), size=n)
        di = rng.integers(0, len(domains), size=n)
        dbi = rng.integers(0, len(dob_arr), size=n)
        values = np.empty(n, dtype=object)
        for k in range(n):
            first = first_arr[fi[k]]
            last = last_arr[li[k]]
            domain = domains[di[k]]
            dob = dob_arr[dbi[k]]
            email = self._email(first, last, domain, fmt)
            # Order matches output_columns (alphabetical): dob, email, first_name, last_name.
            values[k] = (dob, email, first, last)
        values.setflags(write=False)
        return BundlePool(
            values=values,
            provider=self.composite_name,
            locale=spec.locale or "en_US",
            config_hash="person/v1",
            seed=seed,
            size=n,
            build_time_ms=0.0,
            backend_type="composite",
            backend_version="person/v1",
            distinct_count=n,
            output_columns=self.output_columns,
        )

    def _generate_deterministic(
        self, spec: ProviderSpec, source: pd.Series
    ) -> dict[str, pd.Series]:
        assert spec.seed is not None  # noqa: S101 -- guarded by generate_bundle
        first_arr, last_arr, dob_arr = self._pools(spec)
        domains = self._resolve_domains(spec)
        fmt = str(spec.extra.get("email_format", _DEFAULT_EMAIL_FORMAT))
        for pool_len in (len(first_arr), len(last_arr), len(dob_arr), len(domains)):
            self._check_pool_size(pool_len)

        first_vals: list[Any] = []
        last_vals: list[Any] = []
        email_vals: list[Any] = []
        dob_vals: list[Any] = []
        is_null = source.isna()
        for i in range(len(source)):
            if is_null.iloc[i]:
                first_vals.append(pd.NA)
                last_vals.append(pd.NA)
                email_vals.append(pd.NA)
                dob_vals.append(pd.NA)
                continue
            canonical = _canonicalize_source(source.iloc[i])
            latent = derive(spec.seed, self.coherent_namespace, canonical)
            # 32 bytes / 4 outputs = 8 bytes per slot.
            first_idx = int.from_bytes(latent[0:8], "big") % len(first_arr)
            last_idx = int.from_bytes(latent[8:16], "big") % len(last_arr)
            domain_idx = int.from_bytes(latent[16:24], "big") % len(domains)
            dob_idx = int.from_bytes(latent[24:32], "big") % len(dob_arr)
            first = first_arr[first_idx]
            last = last_arr[last_idx]
            domain = domains[domain_idx]
            first_vals.append(first)
            last_vals.append(last)
            email_vals.append(self._email(first, last, domain, fmt))
            dob_vals.append(dob_arr[dob_idx])
        return {
            "first_name": pd.Series(first_vals),
            "last_name": pd.Series(last_vals),
            "email": pd.Series(email_vals),
            "dob": pd.Series(dob_vals),
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
                    message="Deterministic composite_person requires source + seed.",
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


def composite_person(
    *,
    coherent_namespace: str,
    registry: "ProviderRegistry | None" = None,
    pool_size: int = 10_000,
) -> CompositePerson:
    """Construct a CompositePerson generator bound to `coherent_namespace`."""
    return CompositePerson(
        coherent_namespace=coherent_namespace,
        registry=registry,
        pool_size=pool_size,
    )


__all__: list[str] = ["CompositePerson", "composite_person"]
