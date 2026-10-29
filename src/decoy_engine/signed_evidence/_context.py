"""VerificationContext: the SOLE source of trusted expectations.

Nothing the verifier compares against is ever derived from the bundle under test. A valid
signature for one job therefore cannot be replayed as evidence for another (plan 1.5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ._catalog import RequiredCatalog
from ._errors import SignedEvidenceError
from ._trust import TrustPolicy

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MEDIA = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")


class Purpose(Enum):
    #: A new release is being authorized now: the signer must be currently active.
    RELEASE_GATE = "RELEASE_GATE"
    #: A recipient is deciding whether to accept an existing bundle: retired (not revoked) is fine.
    CONSUMER_ACCEPTANCE = "CONSUMER_ACCEPTANCE"
    #: An inspection of past evidence. Never returns full success, never current clearance.
    HISTORICAL_AUDIT = "HISTORICAL_AUDIT"

    @property
    def strict(self) -> bool:
        return self is not Purpose.HISTORICAL_AUDIT


@dataclass(frozen=True, slots=True)
class ExpectedArtifact:
    id: str
    sha256: str
    media_type: str

    def __post_init__(self) -> None:
        if not (
            _ID.fullmatch(self.id)
            and _DIGEST.fullmatch(self.sha256)
            and _MEDIA.fullmatch(self.media_type)
        ):
            raise SignedEvidenceError("CONTEXT_INVALID")


@dataclass(frozen=True, slots=True)
class HistoricalDecision:
    """An operator-supplied, externally authenticated decision binding one exact bundle.

    The verifier cannot authenticate the archive itself; supplying it in the context is the
    operator's assertion that it did.
    """

    statement_sha256: str
    signature_sha256: str
    decision_ref: str
    policy_digest: str

    def __post_init__(self) -> None:
        if not (
            _DIGEST.fullmatch(self.statement_sha256)
            and _DIGEST.fullmatch(self.signature_sha256)
            and _DIGEST.fullmatch(self.policy_digest)
            and isinstance(self.decision_ref, str)
            and 0 < len(self.decision_ref) <= 256
        ):
            raise SignedEvidenceError("CONTEXT_INVALID")


@dataclass(frozen=True, slots=True)
class VerificationContext:
    purpose: Purpose
    trust_policy: TrustPolicy
    required_catalog: RequiredCatalog
    expected_tenant: str | None = None
    expected_bundle_id: str | None = None
    expected_job_id: str | None = None
    expected_attempt_id: str | None = None
    expected_snapshot_id: str | None = None
    expected_artifacts: tuple[ExpectedArtifact, ...] = ()
    historical_decisions: tuple[HistoricalDecision, ...] = ()
    evaluation_time: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, Purpose):
            raise SignedEvidenceError("CONTEXT_INVALID")
        if not isinstance(self.trust_policy, TrustPolicy) or not isinstance(
            self.required_catalog, RequiredCatalog
        ):
            raise SignedEvidenceError("CONTEXT_INVALID")
        for value in (
            self.expected_tenant,
            self.expected_bundle_id,
            self.expected_job_id,
            self.expected_attempt_id,
            self.expected_snapshot_id,
        ):
            if value is not None and not (isinstance(value, str) and _ID.fullmatch(value)):
                raise SignedEvidenceError("CONTEXT_INVALID")
        ids = [a.id for a in self.expected_artifacts]
        if len(set(ids)) != len(ids):
            raise SignedEvidenceError("CONTEXT_INVALID")
        if self.evaluation_time is not None and self.evaluation_time.tzinfo is None:
            raise SignedEvidenceError("CONTEXT_INVALID")
        if self.purpose.strict:
            required = [self.expected_tenant, self.expected_job_id, self.expected_attempt_id]
            if self.required_catalog.has_required_snapshot_check:
                required.append(self.expected_snapshot_id)
            if any(v is None for v in required):
                raise SignedEvidenceError("CONTEXT_INCOMPLETE")
