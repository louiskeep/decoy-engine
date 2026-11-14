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
        fresh_seed,
        resolve_seed,
        seed_bytes_to_int,
        COMPOSITE_SEED_WIDTHS,
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

HKDF is PyCA `cryptography`'s (`_hkdf.py`); the per-source HMAC is stdlib
`hmac` + `hashlib`.
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
from decoy_engine.determinism._seeds import (
    COMPOSITE_SEED_WIDTHS,
    fresh_seed,
    resolve_seed,
    seed_bytes_to_int,
)

__all__ = [
    "COMPOSITE_SEED_WIDTHS",
    "SEED_PROTOCOL_VERSION",
    "DeterminismError",
    "Domain",
    "IdentityDomain",
    "derive",
    "derive_index",
    "derive_value",
    "fresh_seed",
    "resolve_seed",
    "seed_bytes_to_int",
]
