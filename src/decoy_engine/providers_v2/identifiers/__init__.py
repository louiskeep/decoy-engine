"""Custom identifier adapters (engine-v2 S6).

Five concrete `BackendAdapter` instances that swap S4's Faker-bound
fallbacks for the regulated identifiers:

- SSN (SSA POMS blocklist)
- EIN (IRS prefix list)
- NPI (NPPES Luhn check)
- NDC (FDA segment-length variants)
- MRN (configurable digits + alpha prefix)

Per S6 spec §3.1 (PO PQ-call B2): all 5 adapters declare `poolable=False`.
Deterministic mode routes through `derive_value(seed, namespace,
canonical_source, domain=...)` direct (no PoolAdapter wrap).

Per S6 spec §3.5 (PO PQ-call H2): source canonicalization uses S5's
shipped `_canonicalize_source(value) -> bytes` helper from
`decoy_engine.generation.pool._canonicalize` (single envelope across
S5 sampler + S6 direct paths).

Compile-check row 9 (`deterministic_namespace_completeness`) ships
at `decoy_engine.providers_v2.identifiers._validate`.
"""

from __future__ import annotations

from decoy_engine.providers_v2.identifiers._ein import (
    EinAdapter,
    EinDomain,
    EinValidator,
)
from decoy_engine.providers_v2.identifiers._errors import (
    IdentifierError,
    IdentifierFormatError,
)
from decoy_engine.providers_v2.identifiers._mrn import (
    MrnAdapter,
    MrnDomain,
    MrnValidator,
)
from decoy_engine.providers_v2.identifiers._ndc import (
    NdcAdapter,
    NdcDomain,
    NdcValidator,
)
from decoy_engine.providers_v2.identifiers._npi import (
    NpiAdapter,
    NpiDomain,
    NpiValidator,
)
from decoy_engine.providers_v2.identifiers._ssn import (
    SsnAdapter,
    SsnDomain,
    SsnValidator,
)
from decoy_engine.providers_v2.identifiers._validate import (
    deterministic_namespace_completeness,
)

__all__ = [
    "EinAdapter",
    "EinDomain",
    "EinValidator",
    "IdentifierError",
    "IdentifierFormatError",
    "MrnAdapter",
    "MrnDomain",
    "MrnValidator",
    "NdcAdapter",
    "NdcDomain",
    "NdcValidator",
    "NpiAdapter",
    "NpiDomain",
    "NpiValidator",
    "SsnAdapter",
    "SsnDomain",
    "SsnValidator",
    "deterministic_namespace_completeness",
]
