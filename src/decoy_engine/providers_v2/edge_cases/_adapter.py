"""Edge-case provider adapter: one adapter class serving a table of catalogue specs.

The values are a fixed catalogue (``_text_cases.py``, ``_value_cases.py``); the adapter only
orders them. ``generate_batch`` is the pool-build entry point: ``PoolBuilder`` passes the pool
seed in ``spec.seed``, so the pool is a pure function of the job seed and the column config.
That is what lets a ``strategy: faker`` column use these providers (they declare
``poolable=True``); the per-row choice is then the pool sampler's keyed ``derive_index``, as for
the Faker and Mimesis providers, whose capability pattern this follows
(``supports_deterministic=False``, boosted by ``PoolAdapter`` on the pooled path).

Order rule (part of ``backend_version`` ``edge_case/v1``): block ``k`` of a batch is the catalogue
in the order of the ``k``-th ``permutation(n)`` drawn from one generator, so a batch of at least
``n`` values holds every catalogue value. Changing the rule changes every pool, so it bumps the
version.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from decoy_engine import reasons
from decoy_engine.providers_v2._adapter import CapabilityMatrix, ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2.edge_cases._spec import EdgeCaseSpec


def _rng(spec: ProviderSpec) -> np.random.Generator:
    # The one generator construction: seeded on the pool path, from OS entropy otherwise. The
    # seed stays inside this expression (never bound to a name), as the secret census requires.
    return np.random.default_rng(None if spec.seed is None else int.from_bytes(spec.seed, "big"))


class EdgeCaseAdapter:
    """``BackendAdapter`` for one edge-case spec."""

    backend_type: str = "decoy_native"
    backend_version: str = "edge_case/v1"

    def __init__(self, spec: EdgeCaseSpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> EdgeCaseSpec:
        return self._spec

    def _check(self, provider: str) -> None:
        if provider != self._spec.provider:
            raise ProviderError(
                code=reasons.provider.UNKNOWN_PROVIDER,
                message=f"This adapter serves {self._spec.provider}, not {provider!r}.",
            )

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | int | str | None = None,
    ) -> Any:
        self._check(provider)
        if spec.deterministic:
            raise ProviderError(
                code=reasons.provider.CAPABILITY_VIOLATION,
                message=(
                    f"{provider} does not support deterministic direct generation; "
                    "deterministic columns sample its pool."
                ),
            )
        values = self._spec.values
        return values[int(_rng(spec).integers(len(values)))]

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> Sequence[Any]:
        self._check(provider)
        if spec.deterministic:
            raise ProviderError(
                code=reasons.provider.BATCH_DETERMINISTIC_UNSUPPORTED,
                message=f"{provider}: generate_batch does not support deterministic mode.",
            )
        if count < 0:
            raise ValueError(f"{provider}: count must be non-negative.")
        values = self._spec.values
        rng = _rng(spec)
        out: list[Any] = []
        while len(out) < count:
            out.extend(values[i] for i in rng.permutation(len(values)))
        return out[:count]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        self._check(provider)
        return capability_row(self._spec)


def capability_row(spec: EdgeCaseSpec) -> CapabilityMatrix:
    """The pinned row: pooled, locale-independent, not unique, not for keys."""
    return CapabilityMatrix(
        provider=spec.provider,
        backend_type=EdgeCaseAdapter.backend_type,
        backend_version=EdgeCaseAdapter.backend_version,
        supports_deterministic=False,
        supports_uniqueness=False,
        supports_value_reuse=True,
        preserves_source_cardinality=False,
        participates_in_fk_pk=False,
        poolable=True,
        # Empty means locale-independent: every locale is accepted and ignored.
        supported_locales=(),
        supports_coherent_link=False,
        format_regex=None,
        blocklist_validators=(),
        fallback_behavior="fail_plan_compile",
    )
