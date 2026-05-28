"""engine-v2 S8 composite generators: shared-latent coherent multi-column output.

A composite draws multiple output columns from one shared latent so they stay
coherent ("Maria Chen" -> "maria.chen@..."; Chicago city -> an Illinois ZIP).

Public API (S8 spec API summary):

    from decoy_engine.generation.composite import (
        CompositeGenerator,
        CompositeAdapter,
        BundlePool,
        composite_name_email,
        composite_city_state_zip,
        CompositeError,
    )

Determinism is pool-routed (S5 machinery): composite_city_state_zip selects one
locality triple per row via derive_index; composite_name_email does ONE derive
per row, sub-byte-sliced into three coupled pool indices (S8 spec §4).

Spec: docs/v2/sprints/engine-v2/sprint-08-composite-generators.md in decoy-platform.
"""

from __future__ import annotations

from decoy_engine.generation.composite._bundle_pool import BundlePool
from decoy_engine.generation.composite._city_state_zip import (
    CompositeCityStateZip,
    composite_city_state_zip,
    load_locality_table,
)
from decoy_engine.generation.composite._errors import CompositeError
from decoy_engine.generation.composite._generator import (
    CompositeAdapter,
    CompositeGenerator,
    composite_capability,
)
from decoy_engine.generation.composite._name_email import (
    CompositeNameEmail,
    composite_name_email,
)

__all__ = [
    "BundlePool",
    "CompositeAdapter",
    "CompositeCityStateZip",
    "CompositeError",
    "CompositeGenerator",
    "CompositeNameEmail",
    "composite_capability",
    "composite_city_state_zip",
    "composite_name_email",
    "load_locality_table",
]
