"""RFC 5869 HKDF-SHA256 implementation on stdlib hmac + hashlib.

HKDF (HMAC-based Key Derivation Function) is the standard primitive for
deriving per-context keys from a master seed. Used by the determinism
layer to bind each namespace to a unique HMAC key while preserving
within-namespace stability.

Implementation choice: stdlib only. The PyCA `cryptography` package is
NOT a dep of decoy-engine; every existing keyed primitive in the engine
uses `hmac.new(key, msg, hashlib.sha256)`. HKDF on top of stdlib HMAC
is short enough that a 30-line implementation is the right call here
(see `src/decoy_engine/transforms/fpe.py` line 21-23 for the engine's
anti-PyCA design choice that this module preserves).

The implementation is pinned by reference-vector unit tests against
RFC 5869 §A.1 / §A.2 / §A.3. If those tests pass, the implementation
is correct against the published specification.

References:
- RFC 5869 (HKDF): https://datatracker.ietf.org/doc/html/rfc5869
- RFC 2104 (HMAC): https://datatracker.ietf.org/doc/html/rfc2104
"""

from __future__ import annotations

import hashlib
import hmac

_HASH_LEN = 32  # SHA-256 output length in bytes


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """RFC 5869 §2.2: PRK = HMAC-SHA256(salt, IKM).

    Args:
        salt: optional salt value (a non-secret random value). RFC 5869
            allows a zero-length salt (treated as `HashLen` zero bytes);
            the caller is responsible for passing a context-specific salt
            in normal use.
        ikm: input keying material (the master seed).

    Returns:
        32-byte pseudo-random key (PRK) suitable for input to hkdf_expand.
    """
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 §2.3: OKM = T(1) | T(2) | ... | T(N), truncated to `length`.

    T(0) = empty string (zero length)
    T(N) = HMAC-SHA256(PRK, T(N-1) || info || octet(N))

    Args:
        prk: pseudo-random key (output of hkdf_extract; >= 32 bytes).
        info: optional context-specific info string.
        length: desired output length in bytes. Maximum `255 * 32` per RFC
            5869 §2.3.

    Returns:
        `length` bytes of output keying material.

    Raises:
        ValueError if `length` exceeds RFC 5869's upper bound.
    """
    if length > 255 * _HASH_LEN:
        raise ValueError(f"HKDF length {length} exceeds RFC 5869 maximum {255 * _HASH_LEN}")
    n = (length + _HASH_LEN - 1) // _HASH_LEN
    t = b""
    okm_chunks: list[bytes] = []
    for i in range(1, n + 1):
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm_chunks.append(t)
    return b"".join(okm_chunks)[:length]


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 one-shot: Extract + Expand.

    Equivalent to `hkdf_expand(hkdf_extract(salt, ikm), info, length)`.
    """
    return hkdf_expand(hkdf_extract(salt, ikm), info, length)
