"""Independent result dimensions (plan 1.5). A valid signature is one field among several."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class SchemaStatus(Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    MALFORMED = "MALFORMED"
    NON_CANONICAL = "NON_CANONICAL"


class SignatureStatus(Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNSIGNED = "UNSIGNED"
    NOT_EVALUATED = "NOT_EVALUATED"


class TrustStatus(Enum):
    TRUSTED = "TRUSTED"
    UNKNOWN_KEY = "UNKNOWN_KEY"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    RETIRED = "RETIRED"
    REVOKED = "REVOKED"
    STALE_POLICY = "STALE_POLICY"
    NOT_EVALUATED = "NOT_EVALUATED"


class ArtifactStatus(Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    UNEXPECTED = "UNEXPECTED"
    WITHHELD = "WITHHELD"
    NOT_EVALUATED = "NOT_EVALUATED"


class CheckStatus(Enum):
    PASS = "PASS"  # noqa: S105 -- a verdict name, not a credential
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_EVALUATED = "NOT_EVALUATED"


class IdentityBinding(Enum):
    BOUND = "BOUND"
    MISMATCH = "MISMATCH"
    NOT_ASSERTED = "NOT_ASSERTED"


class ReleaseVerdict(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class HistoricalAuthorization(Enum):
    ESTABLISHED = "ESTABLISHED"
    DISALLOWED = "DISALLOWED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ExitClass(Enum):
    """Symbolic exit classes. Numeric process codes are assigned by the CLI after it inspects
    its existing exit contract; only OK is full success."""

    OK = "OK"
    PARTIAL = "PARTIAL"
    UNTRUSTED = "UNTRUSTED"
    TAMPERED = "TAMPERED"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED = "BLOCKED"
    MALFORMED = "MALFORMED"
    UNSUPPORTED = "UNSUPPORTED"

    @property
    def is_success(self) -> bool:
        return self is ExitClass.OK


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    id: str
    status: ArtifactStatus
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    subject_kind: str
    subject_id: str
    required: bool
    outcome: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "subject": {"kind": self.subject_kind, "id": self.subject_id},
            "required": self.required,
            "outcome": self.outcome,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TimeInfo:
    """``created_at`` is signer-asserted and verbatim; ``evaluation_time`` is the verifier's own."""

    created_at_untrusted: str | None
    evaluation_time: datetime


@dataclass(frozen=True, slots=True)
class VerificationResult:
    statement_id: str
    schema: SchemaStatus
    signature: SignatureStatus
    trust: TrustStatus
    artifacts: ArtifactStatus
    checks: CheckStatus
    identity: IdentityBinding
    release: ReleaseVerdict
    historical_authorization: HistoricalAuthorization
    exit_class: ExitClass
    publication_state: str | None
    purpose: str
    time: TimeInfo
    trust_policy_digest: str
    catalog_digest: str
    reasons: tuple[str, ...]
    notes: tuple[str, ...]
    artifact_results: tuple[ArtifactResult, ...]
    check_results: tuple[CheckResult, ...]
    unbound_files: tuple[str, ...]
    signer: str | None = None
    key_version: str | None = None
    historical_evidence_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id,
            "schema": self.schema.value,
            "signature": self.signature.value,
            "trust": self.trust.value,
            "artifacts": self.artifacts.value,
            "checks": self.checks.value,
            "identity": self.identity.value,
            "publication": {"signed_state": self.publication_state, "release": self.release.value},
            "historical_authorization": self.historical_authorization.value,
            "exit_class": self.exit_class.value,
            "purpose": self.purpose,
            "time": {
                "created_at_untrusted": self.time.created_at_untrusted,
                "evaluation_time": self.time.evaluation_time.isoformat(),
            },
            "trust_policy_digest": self.trust_policy_digest,
            "catalog_digest": self.catalog_digest,
            "signer": self.signer,
            "key_version": self.key_version,
            "historical_evidence_ref": self.historical_evidence_ref,
            "reasons": list(self.reasons),
            "notes": list(self.notes),
            "artifact_results": [a.to_dict() for a in self.artifact_results],
            "check_results": [c.to_dict() for c in self.check_results],
            "unbound_files": list(self.unbound_files),
        }
