"""The signing boundary: the only place a signed evidence carrier is constructed.

Pattern: Sigstore's ``signature.Signer`` seam (resolve one key version, sign, verify the result
locally before use) and the in-toto/DSSE rule that the signed payload is the exact canonical
statement, never a digest of an older manifest. ``sign_evidence`` validates the draft, resolves the
signer identity once, signs the detached canonical statement through the JOSE layer, and checks its
own output against the signer's advertised public key before returning; a provider that fails,
times out, or returns a malformed, wrong-key, wrong-curve, DER or double-hashed signature yields no
carrier. There is no unsigned fallback. ``adopt_verified_evidence`` is the only way existing
evidence enters a carrier: it re-verifies and preserves the original bytes, signer and key
version; it never re-signs history under today's key.

The carrier constructor is guarded by a module-private token so it is not a public dictionary
conversion. Python cannot stop code that imports the token; the signing choke-point sentry
(``tests/sentry/test_evidence_signing_boundary.py``) scans the whole tree for any such use.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import _jws
from ._canonical import canonical_bytes
from ._context import VerificationContext
from ._envelope import ALG, CTY, TYP, parse_signature_object
from ._errors import SignedEvidenceError
from ._result import SchemaStatus, SignatureStatus, TrustStatus
from ._signer import ResolvedSigner
from ._statement import EvidenceStatementV1, build_statement_bytes, parse_statement_ex
from ._verifier import verify_signed_evidence

EVIDENCE_FILENAME = "evidence.json"
SIGNATURE_FILENAME = "evidence.sig.json"

_CARRIER_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class SignedEvidence:
    """Immutable statement and signature bytes plus the facts a release permit binds to."""

    statement: bytes
    signature: bytes
    statement_id: str
    signer: str
    key_version: str
    publication_state: str
    job_id: str
    attempt_id: str
    inventory_digest: str

    def __init__(self, *args: Any, **fields: Any) -> None:
        if not args or args[0] is not _CARRIER_TOKEN or len(args) != 1:
            raise SignedEvidenceError("CARRIER_CONSTRUCTION_FORBIDDEN")
        names = {f.name for f in dataclasses.fields(self)}
        if set(fields) != names:
            raise SignedEvidenceError("CARRIER_FIELDS_INVALID")
        if not (isinstance(fields["statement"], bytes) and isinstance(fields["signature"], bytes)):
            raise SignedEvidenceError("CARRIER_FIELDS_INVALID")
        for name, value in fields.items():
            object.__setattr__(self, name, value)

    def __reduce__(self) -> Any:
        # A pickle would rebuild the object without its constructor guard. Persist the bytes and
        # re-verify them with ``adopt_verified_evidence`` instead.
        raise TypeError("signed evidence carriers are not serialisable; persist the bytes")

    def __copy__(self) -> Any:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return self

    def files(self) -> tuple[tuple[str, bytes], tuple[str, bytes]]:
        """The two files that make up a published statement, in write order."""
        return (EVIDENCE_FILENAME, self.statement), (SIGNATURE_FILENAME, self.signature)


def inventory_digest(statement: EvidenceStatementV1) -> str:
    """SHA-256 over the canonical artifact inventory, the same value a release permit binds."""
    inventory = [
        {
            "id": a.id,
            "path": a.path,
            "media_type": a.media_type,
            "bytes": a.bytes,
            "sha256": a.sha256,
            "disposition": a.disposition,
        }
        for a in statement.artifacts
    ]
    return "sha256:" + hashlib.sha256(canonical_bytes(inventory)).hexdigest()


def _carrier(
    statement_bytes: bytes,
    signature: bytes,
    parsed: EvidenceStatementV1,
    *,
    signer: str,
    version: str,
) -> SignedEvidence:
    return SignedEvidence(
        _CARRIER_TOKEN,
        statement=statement_bytes,
        signature=signature,
        statement_id=parsed.statement_id,
        signer=signer,
        key_version=version,
        publication_state=parsed.publication.state,
        job_id=parsed.job_id,
        attempt_id=parsed.attempt_id,
        inventory_digest=inventory_digest(parsed),
    )


def sign_evidence(draft: Mapping[str, Any], *, signer: ResolvedSigner) -> SignedEvidence:
    """Validate, canonicalize and sign ``draft``; return an immutable, locally verified carrier."""
    statement = build_statement_bytes(draft)  # raises before the signer is consulted
    parsed, _ = parse_statement_ex(statement)
    identity = signer.identity  # resolved exactly once: an alias cannot rotate mid-flight
    if parsed.signer != identity.signer:
        raise SignedEvidenceError("SIGNER_IDENTITY_MISMATCH")
    header = {
        "alg": ALG,
        "kid": identity.kid,
        "typ": TYP,
        "cty": CTY,
        "crit": ["decoy_signer", "decoy_key_version"],
        "decoy_signer": identity.signer,
        "decoy_key_version": identity.key_version,
    }
    try:
        protected, signature_segment = _jws.sign_detached(
            protected_header=header, statement=statement, signer=signer
        )
        public_jwk = dict(signer.public_jwk())
    except SignedEvidenceError:
        raise
    except Exception:
        raise SignedEvidenceError("SIGNING_FAILED") from None

    signature = json.dumps(
        {"protected": protected, "signature": signature_segment}, separators=(",", ":")
    ).encode("ascii")
    envelope = parse_signature_object(signature)
    if (envelope.kid, envelope.decoy_signer, envelope.decoy_key_version) != (
        identity.kid,
        identity.signer,
        identity.key_version,
    ):
        raise SignedEvidenceError("SIGNER_IDENTITY_MISMATCH")
    if not _jws.verify_es256_signature(
        protected_segment=protected,
        payload_segment=_jws.b64u_encode(statement),
        signature_segment=signature_segment,
        public_jwk=public_jwk,
    ):
        raise SignedEvidenceError("SIGNER_BAD_SIGNATURE")
    return _carrier(
        statement, signature, parsed, signer=identity.signer, version=identity.key_version
    )


def adopt_verified_evidence(
    *, statement: bytes, signature: bytes | None, context: VerificationContext
) -> SignedEvidence:
    """Wrap EXISTING evidence after full verification, preserving its bytes and original signer.

    Requires a valid signature by a key that is active or retired (not revoked, not from a stale
    snapshot) and an identity that does not contradict the caller's expectations. Artifacts are not
    inspected here: adoption is about the statement and signature bytes.
    """
    if signature is None:
        raise SignedEvidenceError("ADOPT_NOT_VERIFIED")
    result = verify_signed_evidence(
        statement=statement, signature=signature, artifact_root=None, context=context
    )
    if not (
        result.schema is SchemaStatus.SUPPORTED
        and result.signature is SignatureStatus.VALID
        and result.trust in (TrustStatus.TRUSTED, TrustStatus.RETIRED)
        and result.identity.name != "MISMATCH"
    ):
        raise SignedEvidenceError("ADOPT_NOT_VERIFIED")
    parsed, _ = parse_statement_ex(statement)
    envelope = parse_signature_object(signature)
    return _carrier(
        bytes(statement),
        bytes(signature),
        parsed,
        signer=envelope.decoy_signer,
        version=envelope.decoy_key_version,
    )
