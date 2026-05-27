"""BackendAdapter protocol + CapabilityMatrix + ProviderSpec.

The contract every provider backend implements. At S4 close the only
concrete implementation is `FakerAdapter` (in `_faker_adapter.py`); S5+
add `PoolAdapter`, `DecoyNativeAdapter`, `MimesisAdapter`.

Source patterns:
- Protocol-based adapter contract draws from PEP 544 structural typing
  and Python's `runtime_checkable` Protocol pattern (no inheritance
  required; concrete adapters just implement the method signatures).
- CapabilityMatrix-as-Pydantic-model draws from the same pattern S1's
  `_manifest_schema.py` uses for fixture manifests: registry-load-time
  validation, frozen instances, `extra="forbid"` to catch typos.
- ProviderSpec frozen-dataclass shape mirrors S3's `Domain` and S2's
  `NamespaceBinding`: immutable inputs to pure functions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from decoy_engine.providers_v2._errors import ProviderError


@dataclass(frozen=True)
class ProviderSpec:
    """The structured argument every adapter call carries.

    Replaces the unstructured-dict `provider_config:` block from the
    plan with a typed wrapper. The planner constructs this from the
    plan's per-column block at execute-time (or earlier at pool-build
    time for S5).

    `__post_init__` runs three defensive checks (per S4 spec §3):

    1. `deterministic=True` requires both `namespace` and `seed` non-None.
    2. When `seed is not None`, `len(seed) == 8` (matches the job-seed
       bytes form per S3 spec §5.5).
    3. `deterministic=True` requires `namespace` to be non-empty.

    `extra` is a `dict[str, Any]` for provider-specific knobs. By
    convention callers treat it as read-only; mutation is undefined
    behavior. `ProviderSpec` is unhashable as a consequence (dict
    fields are unhashable); callers that need a cache key derive one
    via canonical-JSON serialization (see S5's PoolBuilder pattern).
    """

    locale: str | None
    deterministic: bool
    namespace: str | None
    seed: bytes | None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.deterministic:
            if self.namespace is None or self.seed is None:
                raise ProviderError(
                    code="deterministic_requires_namespace_and_seed",
                    message=(
                        "ProviderSpec(deterministic=True) requires both "
                        f"`namespace` and `seed` to be non-None; got "
                        f"namespace={self.namespace!r}, "
                        f"seed={'<bytes>' if self.seed else None!r}."
                    ),
                )
            if self.namespace == "":
                raise ProviderError(
                    code="namespace_empty",
                    message=(
                        "ProviderSpec(deterministic=True) requires `namespace` "
                        "to be non-empty (matches S2's NamespaceRegistry contract)."
                    ),
                )
        if self.seed is not None and len(self.seed) != 8:
            raise ProviderError(
                code="seed_wrong_length",
                message=(
                    f"ProviderSpec.seed must be exactly 8 bytes when set; "
                    f"got {len(self.seed)} bytes. The job seed is 8-byte "
                    "big-endian per S3 spec §5.5."
                ),
            )


class CapabilityMatrix(BaseModel):
    """Per-provider capability declarations consumed by the planner +
    by adapters that route per-provider.

    Pydantic model with `extra="forbid"` to catch typos at registry-
    load time. Every field is required (no defaults); catching "you
    forgot to declare uniqueness for synthetic_ssn" at import time is
    the point. The model is frozen; instances are immutable.

    The 14 fields are pinned per cross-sprint contracts §2.3 and the S4
    spec §4 disambiguation table (which fields S4 fills vs which later
    sprints fill via the same shape).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    backend_type: str
    backend_version: str
    supports_deterministic: bool
    supports_uniqueness: bool
    supports_value_reuse: bool
    preserves_source_cardinality: bool
    participates_in_fk_pk: bool
    poolable: bool
    supported_locales: tuple[str, ...]
    supports_coherent_link: bool
    format_regex: str | None
    blocklist_validators: tuple[str, ...]
    fallback_behavior: str


class BackendAdapter(Protocol):
    """The contract every provider backend implements.

    At S4 close: FakerAdapter is the only concrete implementation. S5
    adds PoolAdapter (wraps another adapter through this same protocol).
    S6 adds DecoyNativeAdapter. S7 adds MimesisAdapter.

    Narrow protocol per best-practices §3.3: library code doesn't know
    its callers. `is_valid`, `format_spec`, `locale_enumeration` are
    intentionally NOT on the protocol; they live on the capability
    matrix or on later-sprint specialized surfaces.
    """

    backend_type: str
    backend_version: str

    def generate(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        source_value: bytes | None = None,
    ) -> Any:
        """Return one masked value. The execute-time direct-generate path.

        `source_value` is non-None when the backend is deterministic-
        capable AND the strategy is in deterministic mode. The adapter
        then routes through `decoy_engine.determinism.derive_value(...)`
        with a Domain bound to the provider + spec. None means
        "non-deterministic; just generate."
        """
        ...

    def generate_batch(
        self,
        provider: str,
        *,
        spec: ProviderSpec,
        count: int,
    ) -> Sequence[Any]:
        """Return `count` values. The hot path for pool builds (S5) and
        for per-column non-deterministic generation.

        Deterministic-mode batches are NOT supported through this
        method; deterministic callers use `generate(...)` per row, or
        call `derive_index(...)` into a pre-built pool (S5 path).
        """
        ...

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        """Return the per-provider capabilities for `provider`.

        Raises `ProviderError(code='unknown_provider')` if this adapter
        does not handle `provider`. The registry pre-filters before
        calling, so this is a defensive check.
        """
        ...
