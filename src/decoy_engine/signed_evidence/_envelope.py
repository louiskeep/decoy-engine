"""SignatureEnvelopeV1: strict parse of the detached flattened JWS object and its protected header.

Pattern: RFC 7515 section 7.2.2 (flattened JSON serialization, detached content per Appendix F)
with the algorithm-restriction, explicit-typing and critical-header rules of RFC 8725 sections
3.1, 3.11 and 3.12. The protected segment is kept exactly as transmitted: verification is over
those bytes, never over a re-serialization. The header is parsed with duplicate-member rejection
so a last-wins decoder cannot disagree with the signer about what was signed.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

from . import _strict_json as sj
from ._errors import SignedEvidenceError

ALG = "ES256"
TYP = "application/vnd.decoy.evidence-signature.v1+jws"
CTY = "application/vnd.decoy.evidence.v1+json"
CRIT = frozenset({"decoy_signer", "decoy_key_version"})
SIGNATURE_BYTES = 64

_MAX_ENVELOPE_BYTES = 16 * 1024
_MAX_PROTECTED_BYTES = 2048
_B64URL = re.compile(r"^[A-Za-z0-9_-]*$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# Members that change who is trusted or how bytes are interpreted; never accepted from a header.
_FORBIDDEN = frozenset(
    {"jwk", "jku", "x5u", "x5c", "x5t", "x5t#S256", "b64", "zip", "epk", "enc", "iv", "tag"}
)
_ALLOWED = frozenset({"alg", "kid", "typ", "cty", "crit", "decoy_signer", "decoy_key_version"})
_REQUIRED = ("alg", "kid", "typ", "cty", "decoy_signer", "decoy_key_version")


@dataclass(frozen=True, slots=True)
class SignatureEnvelopeV1:
    protected_segment: str
    signature_segment: str
    signature: bytes
    kid: str
    decoy_signer: str
    decoy_key_version: str
    envelope_digest: str


def _fail(code: str) -> SignedEvidenceError:
    return SignedEvidenceError(code, status="MALFORMED")


def _decode_segment(segment: str) -> bytes:
    if not _B64URL.fullmatch(segment):
        raise _fail("ENVELOPE_BASE64URL_INVALID")
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except ValueError:
        raise _fail("ENVELOPE_BASE64URL_INVALID") from None
    # Reject non-canonical encodings (nonzero trailing bits) so one value has one spelling.
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != segment:
        raise _fail("ENVELOPE_BASE64URL_INVALID")
    return raw


def _prefixed(exc: SignedEvidenceError, prefix: str, not_json: str) -> SignedEvidenceError:
    code = exc.code
    if code in ("NOT_JSON", "NOT_UTF8", "BOM"):
        return _fail(not_json)
    return _fail(f"{prefix}_{code}")


def parse_signature_object(raw: bytes) -> SignatureEnvelopeV1:
    try:
        doc = sj.parse_strict(raw, max_bytes=_MAX_ENVELOPE_BYTES)
    except SignedEvidenceError as exc:
        raise _prefixed(exc, "ENVELOPE", "ENVELOPE_NOT_JSON") from None
    if not isinstance(doc, dict):
        raise _fail("ENVELOPE_SHAPE_UNSUPPORTED")
    if "header" in doc:
        raise _fail("ENVELOPE_UNPROTECTED_HEADER")
    if "payload" in doc:
        raise _fail("ENVELOPE_PAYLOAD_PRESENT")
    if set(doc) != {"protected", "signature"}:
        raise _fail("ENVELOPE_SHAPE_UNSUPPORTED")
    protected, signature = doc["protected"], doc["signature"]
    if not isinstance(protected, str) or not isinstance(signature, str):
        raise _fail("ENVELOPE_SHAPE_UNSUPPORTED")
    header_bytes = _decode_segment(protected)
    sig_bytes = _decode_segment(signature)
    if len(header_bytes) > _MAX_PROTECTED_BYTES:
        raise _fail("ENVELOPE_TOO_LARGE")

    try:
        header = sj.parse_strict(header_bytes, max_bytes=_MAX_PROTECTED_BYTES)
    except SignedEvidenceError as exc:
        raise _prefixed(exc, "ENVELOPE", "ENVELOPE_PROTECTED_NOT_JSON") from None
    if not isinstance(header, dict):
        raise _fail("ENVELOPE_PROTECTED_NOT_JSON")

    if _FORBIDDEN & set(header):
        raise _fail("ENVELOPE_HEADER_MEMBER_FORBIDDEN")
    crit = header.get("crit")
    if (
        not isinstance(crit, list)
        or len(crit) != len(CRIT)
        or not all(isinstance(c, str) for c in crit)
        or set(crit) != CRIT
    ):
        raise _fail("ENVELOPE_CRIT_INVALID")
    if set(header) - _ALLOWED:
        raise _fail("ENVELOPE_HEADER_MEMBER_FORBIDDEN")
    for name in _REQUIRED:
        if name not in header:
            raise _fail("ENVELOPE_HEADER_FIELD_MISSING")
    if header["alg"] != ALG:
        raise _fail("ENVELOPE_ALG_NOT_ES256")
    if header["typ"] != TYP:
        raise _fail("ENVELOPE_TYP_INVALID")
    if header["cty"] != CTY:
        raise _fail("ENVELOPE_CTY_INVALID")
    for name in ("kid", "decoy_signer", "decoy_key_version"):
        value = header[name]
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise _fail("ENVELOPE_HEADER_FIELD_INVALID")
    if len(sig_bytes) != SIGNATURE_BYTES:
        raise _fail("SIGNATURE_ENCODING_INVALID")
    return SignatureEnvelopeV1(
        protected_segment=protected,
        signature_segment=signature,
        signature=sig_bytes,
        kid=header["kid"],
        decoy_signer=header["decoy_signer"],
        decoy_key_version=header["decoy_key_version"],
        envelope_digest="sha256:" + hashlib.sha256(raw).hexdigest(),
    )
