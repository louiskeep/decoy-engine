"""Custom identifier adapters (engine-v2 S6 + MG-1 S4).

Concrete `BackendAdapter` instances that swap S4's Faker-bound
fallbacks for the regulated identifiers:

S6 baseline:
- SSN (SSA POMS blocklist)
- EIN (IRS prefix list)
- NPI (NPPES Luhn check)
- NDC (FDA segment-length variants)
- MRN (configurable digits + alpha prefix)

MG-1 S4 additions (2026-06-01):
- PAN (ISO/IEC 7812-1 Luhn)
- ICD-10 (CDC chapter range)
- IBAN (ISO 13616 mod-97)
- CUSIP (modified Luhn)

Per S6 spec §3.1 (PO PQ-call B2): all adapters declare
`poolable=False`. Deterministic mode routes through
`derive_value(seed, namespace, canonical_source, domain=...)` direct
(no PoolAdapter wrap).

Per S6 spec §3.5 (PO PQ-call H2): source canonicalization uses S5's
shipped `_canonicalize_source(value) -> bytes` helper from
`decoy_engine.generation.pool._canonicalize` (single envelope across
S5 sampler + S6 + MG-1 S4 direct paths).

Compile-check row 9 (`deterministic_namespace_completeness`) ships
at `decoy_engine.providers_v2.identifiers._validate`.
"""

from __future__ import annotations

from decoy_engine.providers_v2.identifiers._cusip import (
    CusipAdapter,
    CusipDomain,
    CusipValidator,
)
from decoy_engine.providers_v2.identifiers._ein import (
    EinAdapter,
    EinDomain,
    EinValidator,
)
from decoy_engine.providers_v2.identifiers._errors import (
    IdentifierError,
    IdentifierFormatError,
)
from decoy_engine.providers_v2.identifiers._iban import (
    IbanAdapter,
    IbanDomain,
    IbanValidator,
)
from decoy_engine.providers_v2.identifiers._icd10 import (
    Icd10Adapter,
    Icd10Domain,
    Icd10Validator,
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
from decoy_engine.providers_v2.identifiers._pan import (
    PanAdapter,
    PanDomain,
    PanValidator,
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
    "CusipAdapter",
    "CusipDomain",
    "CusipValidator",
    "EinAdapter",
    "EinDomain",
    "EinValidator",
    "IbanAdapter",
    "IbanDomain",
    "IbanValidator",
    "Icd10Adapter",
    "Icd10Domain",
    "Icd10Validator",
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
    "PanAdapter",
    "PanDomain",
    "PanValidator",
    "SsnAdapter",
    "SsnDomain",
    "SsnValidator",
    "deterministic_namespace_completeness",
]
