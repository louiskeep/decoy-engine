"""Private ES256 detached-JWS primitives (RFC 7515, RFC 7518 section 3.4).

Pattern: RFC 7515 flattened JSON serialization with detached content (Appendix F), ES256 only,
through ``joserfc``; the JWS signing input is built by the library from the protected segment
exactly as transmitted. The KMS seam follows Sigstore's ``signature.Signer`` split: the external
signer receives only the signing input and returns the fixed-width P1363 signature. Nothing here
implements curve arithmetic, ASN.1 handling or signature normalisation.

These functions answer one mathematical question. They are deliberately underscore-private: an
authorization decision must go through the full Decoy-profile verifier in ``_verifier`` which also
enforces ``crit``, ``typ``/``cty``, signer/key-version identity and trust.
"""

from __future__ import annotations

import base64
from typing import Any, Protocol

from joserfc.errors import JoseError
from joserfc.jwk import ECKey
from joserfc.jws import (
    HeaderMember,
    JWSAlgModel,
    JWSRegistry,
    default_registry,
    deserialize_json,
    sign_flattened_json,
)
from joserfc.registry import HeaderParameter

from ._errors import SignedEvidenceError

ALG = "ES256"
SIGNATURE_BYTES = 64

# Sized for MAX_BYTES (4 MiB) statements: base64url expands by 4/3.
_MAX_PAYLOAD_SEGMENT = 5_700_000
_MAX_HEADER_SEGMENT = 4096
_MAX_SIGNATURE_SEGMENT = 256

_DECOY_HEADERS = {
    "decoy_signer": HeaderParameter("Decoy signer identity", "str"),
    "decoy_key_version": HeaderParameter("Decoy immutable key version", "str"),
}


class SigningInputSigner(Protocol):
    """What the JOSE layer needs from a signer: sign the exact input, return 64 P1363 bytes."""

    def sign_signing_input(self, signing_input: bytes) -> bytes: ...


class _ExternalEs256(JWSAlgModel):
    name = ALG
    description = "ES256 via an external signer (KMS/HSM/local key)"
    recommended = True
    key_type = "EC"

    def check_key(self, key: Any) -> None:
        if not callable(getattr(key, "sign_signing_input", None)):
            raise SignedEvidenceError("SIGNER_INVALID")

    def sign(self, msg: bytes, key: Any) -> bytes:
        sig = key.sign_signing_input(msg)
        if not isinstance(sig, (bytes, bytearray)) or len(sig) != SIGNATURE_BYTES:
            raise SignedEvidenceError("SIGNER_BAD_SIGNATURE")
        return bytes(sig)

    def verify(self, msg: bytes, sig: bytes, key: Any) -> bool:  # pragma: no cover - never used
        raise SignedEvidenceError("SIGNER_VERIFY_UNSUPPORTED")


class _SignRegistry(JWSRegistry):
    # Own tables: JWSRegistry.register writes class-level dicts, so a shared registry would be
    # replaced process-wide.
    algorithms: dict[str, JWSAlgModel] = {}
    recommended: list[str] = []
    max_header_length = _MAX_HEADER_SEGMENT
    max_payload_length = _MAX_PAYLOAD_SEGMENT
    max_signature_length = _MAX_SIGNATURE_SEGMENT


class _VerifyRegistry(JWSRegistry):
    algorithms: dict[str, JWSAlgModel] = {}
    recommended: list[str] = []
    max_header_length = _MAX_HEADER_SEGMENT
    max_payload_length = _MAX_PAYLOAD_SEGMENT
    max_signature_length = _MAX_SIGNATURE_SEGMENT


_SignRegistry.register(_ExternalEs256())
_VerifyRegistry.register(default_registry.get_alg(ALG))


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_detached(
    *, protected_header: dict[str, Any], statement: bytes, signer: SigningInputSigner
) -> tuple[str, str]:
    """Return ``(protected_segment, signature_segment)`` for the detached canonical statement."""
    registry = _SignRegistry(
        header_registry=_DECOY_HEADERS, algorithms=[ALG], strict_check_header=True
    )
    try:
        out = sign_flattened_json(
            HeaderMember(protected=protected_header), statement, registry, lambda _m: signer
        )
    except SignedEvidenceError:
        raise
    except JoseError:
        raise SignedEvidenceError("SIGNING_FAILED") from None
    return out["protected"], out["signature"]


def verify_es256_signature(
    *,
    protected_segment: str,
    payload_segment: str,
    signature_segment: str,
    public_jwk: dict[str, Any],
) -> bool:
    """Mathematical ES256 check over the transmitted protected segment and payload segment."""
    registry = _VerifyRegistry(
        header_registry=_DECOY_HEADERS, algorithms=[ALG], strict_check_header=True
    )
    try:
        key = ECKey.import_key(public_jwk)
        deserialize_json(
            {
                "protected": protected_segment,
                "payload": payload_segment,
                "signature": signature_segment,
            },
            key,
            registry=registry,
        )
    except (JoseError, ValueError, TypeError, KeyError):
        return False
    return True
