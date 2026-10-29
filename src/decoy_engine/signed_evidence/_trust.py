"""TrustPolicy: operator-pinned public keys, their scope, and their lifecycle.

Pattern: TUF / Sigstore-style explicit trust roots (RFC 8725 section 3.12 on context
validation): a key is trusted only because the recipient pinned the policy that lists it, by
digest, out of band. A key carried inside the bundle is never a candidate for trust, and ``kid``
alone proves nothing. Key identity is the RFC 7638 JWK thumbprint, computed by the JOSE library.

Lifecycle keeps three questions apart: may this key sign now (ACTIVE), may it still verify
(ACTIVE or RETIRED), and has it been revoked or compromised (REVOKED). Revocation never rewrites
the bytes or makes a mathematically valid signature "invalid"; it changes the trust answer.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any

from joserfc.jwk import ECKey

from . import _strict_json as sj
from ._errors import SignedEvidenceError
from ._time import parse_utc

SCHEMA_ID = "decoy.trust-policy/v1"
MAX_KEYS = 1000
MAX_POLICY_BYTES = 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_KEY_MEMBERS = (
    "tenant",
    "signer",
    "kid",
    "key_version",
    "alg",
    "jwk_thumbprint",
    "jwk",
    "state",
    "retired_at",
    "revoked_at",
    "compromised_since",
)


class KeyState(Enum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class TrustedKey:
    tenant: str
    signer: str
    kid: str
    key_version: str
    jwk: Mapping[str, str]
    thumbprint: str
    state: KeyState
    retired_at: datetime | None
    revoked_at: datetime | None
    compromised_since: datetime | None


@dataclass(frozen=True, slots=True)
class TrustPolicy:
    id: str
    version: str
    issued_at: datetime
    expires_at: datetime
    keys: tuple[TrustedKey, ...]
    digest: str

    def lookup(self, kid: str) -> TrustedKey | None:
        for key in self.keys:
            if key.kid == kid:
                return key
        return None

    @classmethod
    def from_bytes(cls, raw: bytes, *, pinned_sha256: str) -> TrustPolicy:
        """Load a policy only if its digest equals the digest the operator pinned out of band."""
        if not isinstance(pinned_sha256, str) or not _DIGEST.fullmatch(pinned_sha256):
            raise SignedEvidenceError("TRUST_POLICY_PIN_INVALID")
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(actual, pinned_sha256):
            raise SignedEvidenceError("TRUST_POLICY_DIGEST_MISMATCH")
        try:
            doc = sj.parse_strict(raw, max_bytes=MAX_POLICY_BYTES)
        except SignedEvidenceError as exc:
            raise SignedEvidenceError(f"TRUST_POLICY_{exc.code}") from None
        return _build(doc, actual)


def _bad(code: str) -> SignedEvidenceError:
    return SignedEvidenceError(code)


def _ts(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    parsed = parse_utc(value)
    if parsed is None:
        raise _bad("TRUST_POLICY_SCHEMA_INVALID")
    return parsed


def _ident(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise _bad("TRUST_POLICY_SCHEMA_INVALID")
    return value


def _key(node: Any) -> TrustedKey:
    if not isinstance(node, dict) or set(node) != set(_KEY_MEMBERS):
        raise _bad("TRUST_POLICY_SCHEMA_INVALID")
    if node["alg"] != "ES256":
        raise _bad("TRUST_POLICY_ALG_UNSUPPORTED")
    jwk = node["jwk"]
    if (
        not isinstance(jwk, dict)
        or set(jwk) != {"kty", "crv", "x", "y"}
        or jwk["kty"] != "EC"
        or jwk["crv"] != "P-256"
        or not all(isinstance(jwk[m], str) and _B64URL.fullmatch(jwk[m]) for m in ("x", "y"))
    ):
        raise _bad("TRUST_POLICY_KEY_INVALID")
    try:
        thumb = ECKey.import_key(dict(jwk)).thumbprint()
    except (ValueError, TypeError, KeyError):
        raise _bad("TRUST_POLICY_KEY_INVALID") from None
    declared = node["jwk_thumbprint"]
    if not isinstance(declared, str) or not hmac.compare_digest(thumb, declared):
        raise _bad("TRUST_POLICY_THUMBPRINT_MISMATCH")
    if node["state"] not in KeyState.__members__:
        raise _bad("TRUST_POLICY_SCHEMA_INVALID")
    state = KeyState[node["state"]]
    retired = _ts(node["retired_at"], nullable=True)
    revoked = _ts(node["revoked_at"], nullable=True)
    compromised = _ts(node["compromised_since"], nullable=True)
    coherent = {
        KeyState.ACTIVE: retired is None and revoked is None and compromised is None,
        KeyState.RETIRED: retired is not None and revoked is None and compromised is None,
        KeyState.REVOKED: revoked is not None,
    }[state]
    if not coherent:
        raise _bad("TRUST_POLICY_LIFECYCLE_INCOHERENT")
    return TrustedKey(
        tenant=_ident(node["tenant"]),
        signer=_ident(node["signer"]),
        kid=_ident(node["kid"]),
        key_version=_ident(node["key_version"]),
        jwk=MappingProxyType(dict(jwk)),
        thumbprint=thumb,
        state=state,
        retired_at=retired,
        revoked_at=revoked,
        compromised_since=compromised,
    )


def _build(doc: Any, digest: str) -> TrustPolicy:
    if not isinstance(doc, dict) or set(doc) != {
        "schema",
        "id",
        "version",
        "issued_at",
        "expires_at",
        "keys",
    }:
        raise _bad("TRUST_POLICY_SCHEMA_INVALID")
    if doc["schema"] != SCHEMA_ID:
        raise _bad("TRUST_POLICY_SCHEMA_UNSUPPORTED")
    issued, expires = _ts(doc["issued_at"]), _ts(doc["expires_at"])
    if issued is None or expires is None or expires <= issued:
        raise _bad("TRUST_POLICY_SCHEMA_INVALID")
    nodes = doc["keys"]
    if not isinstance(nodes, list) or not nodes or len(nodes) > MAX_KEYS:
        raise _bad("TRUST_POLICY_SCHEMA_INVALID")
    keys = tuple(_key(n) for n in nodes)
    if len({k.kid for k in keys}) != len(keys):
        raise _bad("TRUST_POLICY_DUPLICATE_KID")
    scoped = [(k.tenant, k.signer, k.key_version) for k in keys]
    if len(set(scoped)) != len(scoped) or len({k.thumbprint for k in keys}) != len(keys):
        raise _bad("TRUST_POLICY_KEY_CONFLICT")
    return TrustPolicy(
        id=_ident(doc["id"]),
        version=_ident(doc["version"]),
        issued_at=issued,
        expires_at=expires,
        keys=keys,
        digest=digest,
    )


@dataclass(frozen=True, slots=True)
class TrustEvaluation:
    status: str  # a TrustStatus member name; kept a string so this module stays result-agnostic
    reasons: tuple[str, ...]


def evaluate_trust(
    *,
    policy: TrustPolicy,
    key: TrustedKey,
    header_signer: str,
    header_key_version: str,
    statement_tenant: str,
    statement_signer: str,
    expected_tenant: str | None,
    evaluation_time: datetime,
) -> TrustEvaluation:
    """Answer: is this verified signature, by this pinned key, acceptable for this scope now?"""
    if evaluation_time >= policy.expires_at:
        return TrustEvaluation("STALE_POLICY", ("TRUST_POLICY_STALE",))
    if evaluation_time < policy.issued_at:
        return TrustEvaluation("STALE_POLICY", ("TRUST_POLICY_NOT_YET_VALID",))
    mismatches: list[str] = []
    if header_signer != key.signer or statement_signer != header_signer:
        mismatches.append("SIGNER_MISMATCH")
    if header_key_version != key.key_version:
        mismatches.append("KEY_VERSION_MISMATCH")
    if statement_tenant != key.tenant or (
        expected_tenant is not None and statement_tenant != expected_tenant
    ):
        mismatches.append("TENANT_MISMATCH")
    if mismatches:
        return TrustEvaluation("SCOPE_MISMATCH", tuple(mismatches))
    if key.state is KeyState.REVOKED:
        return TrustEvaluation("REVOKED", ("SIGNER_REVOKED",))
    if key.state is KeyState.RETIRED:
        return TrustEvaluation("RETIRED", ("SIGNER_RETIRED",))
    return TrustEvaluation("TRUSTED", ())


def evaluate_historical(
    *,
    trust_status: str,
    key: TrustedKey | None,
    created_at: datetime | None,
    statement_digest: str,
    signature_digest: str,
    decisions: tuple[Any, ...],
) -> tuple[str, str | None, tuple[str, ...]]:
    """Historical authorization is separate from current trust and never inferred from created_at.

    Returns ``(status, evidence_ref, reasons)``. ``created_at`` is signer-asserted: it can be used
    to refuse (a claim of signing after revocation) but never to grant (a compromised signer can
    backdate). Only an operator-supplied decision bound to this exact statement and signature
    establishes history.
    """
    if trust_status not in ("REVOKED", "STALE_POLICY"):
        return "NOT_APPLICABLE", None, ()
    for d in decisions:
        if d.statement_sha256 == statement_digest and d.signature_sha256 == signature_digest:
            return "ESTABLISHED", d.decision_ref, ("HISTORICAL_DECISION_ESTABLISHED",)
    if trust_status == "REVOKED" and key is not None and created_at is not None:
        if key.revoked_at is not None and created_at >= key.revoked_at:
            return "DISALLOWED", None, ("CREATED_AFTER_REVOCATION",)
        if key.compromised_since is not None and created_at >= key.compromised_since:
            return "DISALLOWED", None, ("CREATED_DURING_COMPROMISE",)
    return "UNKNOWN", None, ("HISTORICAL_AUTHORIZATION_UNPROVEN",)
