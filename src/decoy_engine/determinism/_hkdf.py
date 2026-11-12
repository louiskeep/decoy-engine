"""RFC 5869 HKDF-SHA256 for the engine's per-namespace and per-purpose keys.

Pattern: RFC 5869 HKDF-SHA256 (PyCA cryptography, Apache-2.0/BSD). See:
https://datatracker.ietf.org/doc/html/rfc5869 and
https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf

The arithmetic is PyCA's `HKDF` / `HKDFExpand` (`HKDF.extract` for the extract step); this module only keeps the engine's
three entry points and one application rule over the primitive: an empty salt is refused
(QA-10 F12). RFC 5869 §2.2 and PyCA both treat `salt=b""` as `HashLen` zero bytes, which would
let a quietly failed salt step degrade to the all-zero salt; a caller that wants that salt passes
`b"\\x00" * 32` explicitly. The refusal is repeated in the one-shot because it no longer passes
through `hkdf_extract`. PyCA takes `salt` and `info` only as `bytes`; they are copied to `bytes`
so a bytes-like argument derives what it did before.

Pinned by the RFC 5869 §A.1-A.3 vectors and by a corpus captured from the earlier stdlib
implementation (`tests/fixtures/hkdf/engine-vectors.json`), so every derived key is unchanged.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

_HASH_LEN = 32  # SHA-256 output length in bytes
_MAX_LENGTH = 255 * _HASH_LEN

_EMPTY_SALT_MESSAGE = (
    "hkdf_extract: salt must be non-empty. Pass an application-"
    "specific context string. If you genuinely want the RFC 5869 "
    "§2.2 all-zero default, pass b'\\x00' * 32 explicitly."
)


def _check_length(length: int) -> None:
    if length > _MAX_LENGTH:
        raise ValueError(f"HKDF length {length} exceeds RFC 5869 maximum {_MAX_LENGTH}")


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """RFC 5869 §2.2: PRK = HMAC-SHA256(salt, IKM).

    Args:
        salt: required NON-EMPTY salt (QA-10 F12; pass `b"\\x00" * 32` for the RFC default).
        ikm: input keying material (the master seed).

    Returns:
        32-byte pseudo-random key (PRK) suitable for input to hkdf_expand.

    Raises:
        ValueError: if `salt` is empty.
    """
    try:
        if not salt:
            raise ValueError(_EMPTY_SALT_MESSAGE)
        return HKDF.extract(SHA256(), bytes(salt), ikm)
    except BaseException:
        del ikm
        raise


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 §2.3: OKM = T(1) | T(2) | ... truncated to `length` (at most `255 * 32`).

    Raises:
        ValueError: if `length` exceeds RFC 5869's upper bound.
    """
    try:
        _check_length(length)
        return HKDFExpand(algorithm=SHA256(), length=length, info=bytes(info)).derive(prk)
    except BaseException:
        del prk
        raise


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 one-shot: Extract + Expand, equal to `hkdf_expand(hkdf_extract(salt, ikm), ...)`.

    Raises:
        ValueError: if `salt` is empty or `length` exceeds RFC 5869's upper bound.
    """
    try:
        if not salt:
            raise ValueError(_EMPTY_SALT_MESSAGE)
        _check_length(length)
        return HKDF(algorithm=SHA256(), length=length, salt=bytes(salt), info=bytes(info)).derive(
            ikm
        )
    except BaseException:
        del ikm
        raise
