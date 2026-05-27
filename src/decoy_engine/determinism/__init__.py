"""Determinism layer: stable deterministic mapping primitives.

`derive(seed, namespace, source) -> bytes` is the single guarantee this
package makes: same `(seed, namespace, source)` produces byte-identical
output across processes, days, and engine versions while
`SEED_PROTOCOL_VERSION` is unchanged. Every deterministic-mode column
in V2 routes through it.

Public API:

    from decoy_engine.determinism import (
        derive,
        derive_index,
        derive_value,
        Domain,
        IdentityDomain,
        SEED_PROTOCOL_VERSION,
        DeterminismError,
    )

`Domain` is the protocol shape S6's custom identifiers implement.
`IdentityDomain` is the test-fixture concrete implementation S4 + S5
use to wire deterministic-mode integration tests; not a customer-facing
API.

Source patterns:
- HKDF-SHA256 (RFC 5869): https://datatracker.ietf.org/doc/html/rfc5869
- HMAC-SHA256 (RFC 2104): https://datatracker.ietf.org/doc/html/rfc2104

Implementation lives on stdlib `hmac` + `hashlib`; no PyCA `cryptography`
dependency is added (the engine has an explicit anti-PyCA design choice
in `transforms/fpe.py` lines 21-23 that this package preserves).
"""

from __future__ import annotations

from decoy_engine.determinism._derive import (
    SEED_PROTOCOL_VERSION,
    DeterminismError,
    Domain,
    IdentityDomain,
    derive,
    derive_index,
    derive_value,
)

__all__ = [
    "SEED_PROTOCOL_VERSION",
    "DeterminismError",
    "Domain",
    "IdentityDomain",
    "derive",
    "derive_index",
    "derive_value",
]
