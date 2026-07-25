"""CompositeGenerator protocol + CompositeAdapter (engine-v2 S8).

Two distinct things (S8 spec §2):
- `CompositeGenerator` is the per-composite implementation protocol
  (composite_name_email, composite_city_state_zip implement it). It owns
  `generate_bundle` (the multi-column hot path) and `build_pool`.
- `CompositeAdapter` is a thin `BackendAdapter` (S4 Protocol) wrapper so a
  composite registers in `get_default_registry()` under its composite_name and
  the row-2 `unknown_provider` check accepts it. Its single-column
  `generate`/`generate_batch` are meaningless for a composite (composites write
  multiple coherent columns at once), so they raise
  `composite_requires_bundle_path` to make a miswired single-column route fail
  loudly. `capability_matrix` declares `supports_coherent_link=True`,
  `poolable=True`, `supports_deterministic=False` (boosted to True only via the
  bundle pool path), `backend_type="composite"`. No new CapabilityMatrix field
  (R9; `supports_coherent_link` already exists).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NoReturn, Protocol, runtime_checkable

import pandas as pd

from decoy_engine.generation.composite._bundle_pool import BundlePool
from decoy_engine.generation.composite._errors import CompositeError
from decoy_engine.providers_v2._adapter import CapabilityMatrix, ProviderSpec

_COMPOSITE_NAMES: frozenset[str] = frozenset(
    {
        "composite_name_email",
        "composite_city_state_zip",
        # MG-4 (2026-05-31): generic + shipped composite additions.
        "composite_custom",
        "composite_person",
        "composite_address",
        "composite_provider",
    }
)


@runtime_checkable
class CompositeGenerator(Protocol):
    """The per-composite implementation contract."""

    composite_name: str
    output_columns: tuple[str, ...]
    coherent_namespace: str

    def generate_bundle(
        self,
        spec: ProviderSpec,
        count: int,
        *,
        source: pd.Series | None = ...,
        deterministic: bool = ...,
    ) -> dict[str, pd.Series]:
        """Return a dict mapping output_column -> length-`count` Series, all
        identically indexed (row i is coherent across every output column)."""
        ...

    def build_pool(self, spec: ProviderSpec, *, size: int) -> BundlePool:
        """Build a pool of bundle tuples for pool-backed routing."""
        ...


def composite_capability(provider: str) -> CapabilityMatrix:
    """Build the CapabilityMatrix for a composite provider (registry fold-in)."""
    if provider not in _COMPOSITE_NAMES:
        raise CompositeError(
            code="unknown_composite",
            message=f"{provider!r} is not a composite; known: {sorted(_COMPOSITE_NAMES)!r}.",
        )
    return CapabilityMatrix(
        provider=provider,
        backend_type="composite",
        backend_version="composite/v1",
        supports_deterministic=False,
        supports_uniqueness=True,
        supports_value_reuse=True,
        preserves_source_cardinality=False,
        participates_in_fk_pk=False,
        poolable=True,
        supported_locales=("en_US",),
        supports_coherent_link=True,
        format_regex=None,
        blocklist_validators=(),
        fallback_behavior="fail_plan_compile",
    )


class CompositeAdapter:
    """Thin BackendAdapter wrapper letting a composite register in the registry."""

    backend_type: str = "composite"
    backend_version: str = "composite/v1"

    def __init__(self, composite_name: str) -> None:
        if composite_name not in _COMPOSITE_NAMES:
            raise CompositeError(
                code="unknown_composite",
                message=f"{composite_name!r} is not a composite.",
            )
        self._composite_name = composite_name

    def _reject_single_column(self, provider: str) -> NoReturn:
        raise CompositeError(
            code="composite_requires_bundle_path",
            message=(
                f"{provider!r} is a composite generator; it writes multiple coherent "
                "columns in one pass and cannot be routed through a single-column "
                "generate/generate_batch. Route through CompositeGenerator.generate_bundle."
            ),
        )

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | None = None,
    ) -> Any:
        self._reject_single_column(provider)

    def generate_batch(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        count: int,
    ) -> Sequence[Any]:
        self._reject_single_column(provider)

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return composite_capability(provider)


__all__: list[str] = ["CompositeAdapter", "CompositeGenerator", "composite_capability"]
