"""National identifier providers: one adapter serving a table of specs.

Each spec (``_national_specs.py``) turns 32 bytes into a valid value; this
module supplies the two ways to obtain those bytes and the capability row.

- ``generate(..., deterministic=True)`` keys the bytes on the source value
  with ``derive_value`` (HMAC-SHA256), as the other identifier adapters do.
- ``generate_batch`` draws the bytes from a PCG64 generator seeded with
  ``spec.seed``. ``PoolBuilder`` passes the pool seed there, so a pool is a
  pure function of the job seed and config. That is what lets a
  ``strategy: faker`` column use these providers (they declare
  ``poolable=True``); the per-row choice is then the pool sampler's keyed
  ``derive_index``, exactly as for the Faker providers.

Check digits and validation live only in ``decoy_engine.checksums``.
Construction follows Faker's pattern (random body plus computed check digit)
with python-stdnum and the national specifications for the rules Faker lacks
or gets wrong.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from decoy_engine import checksums, reasons
from decoy_engine.determinism import derive_value
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.providers_v2._adapter import CapabilityMatrix, ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2.identifiers._errors import IdentifierError

_BYTES = 32


@dataclass(frozen=True)
class NationalIdSpec:
    """One national identifier: name, scheme, locales, output shape and builder."""

    provider: str
    scheme: str
    locales: tuple[str, ...]
    format_regex: str
    build: Callable[[bytes], str]
    source: str


@dataclass(frozen=True)
class NationalIdDomain:
    spec: NationalIdSpec

    def from_bytes(self, b: bytes) -> str:
        if len(b) != _BYTES:
            raise IdentifierError(
                code=reasons.provider.INVALID_INPUT_LENGTH,
                message=f"{self.spec.provider}: from_bytes expects {_BYTES} bytes; got {len(b)}.",
            )
        return self.spec.build(b)


@dataclass(frozen=True)
class NationalIdValidator:
    spec: NationalIdSpec

    def is_valid(self, value: str) -> bool:
        return checksums.validate(self.spec.scheme, value)


class NationalIdAdapter:
    """``BackendAdapter`` for one national identifier spec."""

    backend_type: str = "decoy_native"
    backend_version: str = "national_id/v1"

    def __init__(self, spec: NationalIdSpec) -> None:
        self._spec = spec

    def _check(self, provider: str, locale: str | None) -> None:
        if provider != self._spec.provider:
            raise ProviderError(code=reasons.provider.UNKNOWN_PROVIDER, message=f"got {provider!r}")
        if locale is not None and locale not in self._spec.locales:
            raise ProviderError(
                code=reasons.provider.UNSUPPORTED_LOCALE,
                message=(
                    f"{self._spec.provider} supports locales {list(self._spec.locales)}; "
                    f"got locale={locale!r}."
                ),
            )

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        self._check(provider, spec.locale)
        if spec.deterministic and source_value is not None:
            if spec.seed is None:
                raise ProviderError(
                    code=reasons.provider.MISSING_SEED,
                    message=f"{provider}: deterministic mode requires spec.seed.",
                )
            if spec.namespace is None:
                raise ProviderError(
                    code=reasons.provider.MISSING_NAMESPACE,
                    message=f"{provider}: deterministic mode requires spec.namespace.",
                )
            canonical = (
                source_value
                if isinstance(source_value, bytes)
                else _canonicalize_source(source_value)
            )
            return derive_value(
                seed=spec.seed,
                namespace=spec.namespace,
                source=canonical,
                domain=NationalIdDomain(self._spec),
            )
        rng = np.random.default_rng()
        return self._spec.build(rng.bytes(_BYTES))

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        self._check(provider, spec.locale)
        if spec.deterministic:
            raise ProviderError(
                code=reasons.provider.BATCH_DETERMINISTIC_UNSUPPORTED,
                message=f"{provider}: generate_batch does not support deterministic mode.",
            )
        # A seed makes the batch reproducible: PoolBuilder passes the pool seed here.
        rng = (
            np.random.default_rng(int.from_bytes(spec.seed, "big"))
            if spec.seed is not None
            else np.random.default_rng()
        )
        return [self._spec.build(rng.bytes(_BYTES)) for _ in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        self._check(provider, None)
        return capability_row(self._spec)


def capability_row(spec: NationalIdSpec) -> CapabilityMatrix:
    """The pinned row (plan section 4): pooled, not unique, not offered for keys."""
    return CapabilityMatrix(
        provider=spec.provider,
        backend_type=NationalIdAdapter.backend_type,
        backend_version=NationalIdAdapter.backend_version,
        supports_deterministic=True,
        supports_uniqueness=False,
        supports_value_reuse=True,
        preserves_source_cardinality=False,
        participates_in_fk_pk=False,
        poolable=True,
        supported_locales=spec.locales,
        supports_coherent_link=False,
        format_regex=spec.format_regex,
        blocklist_validators=(f"checksums:{spec.scheme}",),
        fallback_behavior="fail_plan_compile",
    )
