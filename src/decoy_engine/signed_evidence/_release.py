"""Release authorization: a permit is minted only after a full verification, never from a flag.

Pattern: policy evaluation separated from authentication (in-toto layout / Sigstore policy), with
a capability object in the style of a fenced token: a ``ReleasePermit`` binds the exact statement
digest, artifact inventory digest, attempt and policy generation, so it cannot be replayed for a
different job, a swapped artifact set or a stale authorization generation. Persisted worker
results, retries, cache hits and exports cannot manufacture one from a Boolean ``signature_valid``
field: the only constructor is guarded by a module-private token and the only caller is the
evaluator below, which re-runs the full verifier.

``DenyAllReleaseEvaluator`` is the default for engine-facing callers until the engine records a
complete check ledger (F33 slice S6). A signed ``REJECTED`` statement never yields a permit.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from typing import Any, Protocol

from ._boundary import SignedEvidence
from ._context import Purpose, VerificationContext
from ._errors import SignedEvidenceError
from ._result import ReleaseVerdict, VerificationResult
from ._verifier import verify_signed_evidence

_PERMIT_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class ReleasePermit:
    """Authorization to expose exactly one verified statement and artifact set."""

    statement_id: str
    inventory_digest: str
    attempt_id: str
    policy_generation: str
    trust_policy_digest: str
    catalog_digest: str

    def __init__(self, *args: Any, **fields: Any) -> None:
        if not args or args[0] is not _PERMIT_TOKEN or len(args) != 1:
            raise SignedEvidenceError("PERMIT_CONSTRUCTION_FORBIDDEN")
        if set(fields) != {f.name for f in dataclasses.fields(self)}:
            raise SignedEvidenceError("PERMIT_FIELDS_INVALID")
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


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    permit: ReleasePermit | None
    reasons: tuple[str, ...]
    result: VerificationResult | None


class ReleaseEvaluator(Protocol):
    def evaluate(
        self,
        signed: SignedEvidence,
        *,
        artifact_root: str | os.PathLike[str] | None,
        context: VerificationContext,
        policy_generation: str,
    ) -> ReleaseDecision: ...


class DenyAllReleaseEvaluator:
    """Authorizes nothing. The safe default until the check ledger and catalog are enforced."""

    def evaluate(
        self,
        signed: SignedEvidence,
        *,
        artifact_root: str | os.PathLike[str] | None,
        context: VerificationContext,
        policy_generation: str,
    ) -> ReleaseDecision:
        return ReleaseDecision(None, ("RELEASE_EVALUATOR_NOT_ENABLED",), None)


class StrictReleaseEvaluator:
    """Mints a permit only when the full verifier returns ``ALLOW`` for a PUBLISHED statement."""

    def evaluate(
        self,
        signed: SignedEvidence,
        *,
        artifact_root: str | os.PathLike[str] | None,
        context: VerificationContext,
        policy_generation: str,
    ) -> ReleaseDecision:
        result = verify_signed_evidence(
            statement=signed.statement,
            signature=signed.signature,
            artifact_root=artifact_root,
            context=context,
        )
        denials: set[str] = set()
        if context.purpose is not Purpose.RELEASE_GATE:
            denials.add("PURPOSE_NOT_RELEASE_GATE")
        if signed.publication_state != "PUBLISHED":
            denials.add("PUBLICATION_STATE_REJECTED")
        if (
            result.statement_id != signed.statement_id
            or context.expected_attempt_id != signed.attempt_id
        ):
            denials.add("CARRIER_CONTEXT_MISMATCH")
        if not policy_generation:
            denials.add("POLICY_GENERATION_MISSING")
        reasons = tuple(sorted(set(result.reasons) | denials))
        if denials or result.release is not ReleaseVerdict.ALLOW:
            return ReleaseDecision(None, reasons, result)
        permit = ReleasePermit(
            _PERMIT_TOKEN,
            statement_id=signed.statement_id,
            inventory_digest=signed.inventory_digest,
            attempt_id=signed.attempt_id,
            policy_generation=policy_generation,
            trust_policy_digest=result.trust_policy_digest,
            catalog_digest=result.catalog_digest,
        )
        return ReleaseDecision(permit, reasons, result)
