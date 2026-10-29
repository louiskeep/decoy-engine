"""The signer seam: resolve one immutable key version, sign the JOSE signing input, return P1363.

Pattern: Sigstore's ``signature.Signer`` / ``PublicKeyProvider`` split (and its in-memory vs cloud
KMS policy): the engine depends on a narrow interface, and a KMS adapter satisfies it without the
engine learning provider details. A provider whose raw API emits ASN.1/DER (AWS KMS ``Sign`` with
``ECDSA_SHA_256`` and ``MessageType=DIGEST``, GCP KMS) converts with ``der_to_p1363``, which is
``cryptography``'s ``decode_dss_signature`` plus fixed-width big-endian encoding. No curve
arithmetic, nonce generation or ASN.1 parsing is implemented here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from joserfc.jwk import ECKey

from ._errors import SignedEvidenceError

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_P256_ORDER = int("ffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551", 16)


@dataclass(frozen=True, slots=True)
class SigningIdentity:
    """Non-secret identity of ONE immutable key version. Never an alias such as "current"."""

    signer: str
    kid: str
    key_version: str

    def __post_init__(self) -> None:
        for v in (self.signer, self.kid, self.key_version):
            if not isinstance(v, str) or not _ID.fullmatch(v):
                raise SignedEvidenceError("SIGNER_IDENTITY_INVALID")


@runtime_checkable
class ResolvedSigner(Protocol):
    """An authorized, already-resolved key version. Resolution happens once, before signing."""

    @property
    def identity(self) -> SigningIdentity: ...

    def public_jwk(self) -> Mapping[str, str]: ...

    def sign_signing_input(self, signing_input: bytes) -> bytes:
        """Sign the exact JWS signing input; return the 64-byte R||S (P1363) signature."""
        ...


def der_to_p1363(der: bytes) -> bytes:
    """Convert a DER ECDSA-Sig-Value to the 64-byte JOSE form, rejecting anything not canonical."""
    try:
        r, s = decode_dss_signature(bytes(der))
    except ValueError:
        raise SignedEvidenceError("SIGNER_BAD_SIGNATURE") from None
    if not (0 < r < _P256_ORDER and 0 < s < _P256_ORDER):
        raise SignedEvidenceError("SIGNER_BAD_SIGNATURE")
    if encode_dss_signature(r, s) != bytes(der):
        raise SignedEvidenceError("SIGNER_BAD_SIGNATURE")  # non-minimal / padded DER
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def public_jwk_of(public_key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    pem = public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    jwk: dict[str, Any] = ECKey.import_key(pem).as_dict(private=False)
    return {k: jwk[k] for k in ("kty", "crv", "x", "y")}


class LocalEs256Signer:
    """An in-process P-256 key. For tests and CLI-local keys; never derived from a job seed."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey, identity: SigningIdentity) -> None:
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise SignedEvidenceError("SIGNER_KEY_INVALID")
        self._key = private_key
        self._identity = identity

    @property
    def identity(self) -> SigningIdentity:
        return self._identity

    def public_jwk(self) -> Mapping[str, str]:
        return public_jwk_of(self._key.public_key())

    def sign_signing_input(self, signing_input: bytes) -> bytes:
        return der_to_p1363(self._key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
