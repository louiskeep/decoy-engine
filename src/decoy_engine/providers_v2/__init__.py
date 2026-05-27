"""Provider registry + Faker adapter (engine-v2 S4).

Public API:

    from decoy_engine.providers_v2 import (
        BackendAdapter,
        ProviderRegistry,
        get_default_registry,
        register_faker_provider_v2,
        ProviderError,
        AdapterError,
        CapabilityMatrix,
        ProviderSpec,
    )

The V2 package ships ALONGSIDE the V1 `decoy_engine.providers` module
per the S4 spec B1 PO call. V1 callers continue to use the V1 module
(`register_faker_provider`, `unregister_faker_provider`, etc.) which
routes through `decoy_engine.internal.faker_setup`. V2 callers (S5+
pool builds, S6+ adapters, planner stamping per H1) consult only the
V2 registry; the V1 + V2 tables stay independent until S9 removes the
V1 strategy stack.

Source patterns:
- BackendAdapter protocol shape: PEP 544 structural typing
  (no inheritance required from concrete adapters).
- CapabilityMatrix Pydantic-model: V1's `_manifest_schema.py` pattern
  (registry-load-time validation, frozen, `extra="forbid"`).
- ProviderRegistry immutable + override-returns-new pattern: best-
  practices §2.1 (validation never mutates input) + the language-server
  URN-registry pattern.

References:
- Spec: docs/v2/sprints/engine-v2/sprint-04-provider-registry-and-faker-adapter.md
  in decoy-platform.
- V2 Engine Operating Model §Provider model: the contract surface this
  package implements.
- Cross-sprint contracts §2.3 (CapabilityMatrix pinned field list),
  §2.11 (BackendAdapter shape), §2.12 (ProviderSpec shape).
"""

from __future__ import annotations

from decoy_engine.providers_v2._adapter import (
    BackendAdapter,
    CapabilityMatrix,
    ProviderSpec,
)
from decoy_engine.providers_v2._errors import AdapterError, ProviderError
from decoy_engine.providers_v2._faker_adapter import (
    FakerAdapter,
    register_faker_provider_v2,
)
from decoy_engine.providers_v2._registry import (
    ProviderRegistry,
    get_default_registry,
)

__all__ = [
    "AdapterError",
    "BackendAdapter",
    "CapabilityMatrix",
    "FakerAdapter",
    "ProviderError",
    "ProviderRegistry",
    "ProviderSpec",
    "get_default_registry",
    "register_faker_provider_v2",
]
