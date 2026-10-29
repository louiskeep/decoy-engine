"""The full Decoy-profile verifier: the only public verification entry.

Pattern: RFC 7515 verification over the received protected segment, then RFC 8725 context
validation (algorithm, ``typ``/``cty``, ``crit``, key identity and scope) against a pinned trust
policy; release authorization is a separate recomputed verdict, so authentication never becomes
accidental authorization. Every expectation comes from the ``VerificationContext``, never from the
bundle under test. Mathematically valid but out-of-profile signatures are rejected here because
the low-level ES256 check in ``_jws`` is private.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import datetime, timezone

from . import _jws
from . import _strict_json as sj
from ._artifacts import bind_artifacts
from ._checks import evaluate_checks
from ._context import Purpose, VerificationContext
from ._envelope import SignatureEnvelopeV1, parse_signature_object
from ._errors import SignedEvidenceError
from ._result import (
    ArtifactResult,
    ArtifactStatus,
    CheckResult,
    CheckStatus,
    ExitClass,
    HistoricalAuthorization,
    IdentityBinding,
    ReleaseVerdict,
    SchemaStatus,
    SignatureStatus,
    TimeInfo,
    TrustStatus,
    VerificationResult,
)
from ._statement import EvidenceStatementV1, parse_statement_ex
from ._time import parse_utc
from ._trust import TrustedKey, evaluate_historical, evaluate_trust

_SCHEMA_BY_STATUS = {
    "UNSUPPORTED": SchemaStatus.UNSUPPORTED,
    "NON_CANONICAL": SchemaStatus.NON_CANONICAL,
}


def _trust_acceptable(trust: TrustStatus, purpose: Purpose) -> bool:
    """Only an active key, or a retired one for a recipient accepting an existing bundle."""
    return trust is TrustStatus.TRUSTED or (
        trust is TrustStatus.RETIRED and purpose is Purpose.CONSUMER_ACCEPTANCE
    )


def _decide(
    *,
    purpose: Purpose,
    schema: SchemaStatus,
    signature: SignatureStatus,
    envelope_malformed: bool,
    trust: TrustStatus,
    artifacts: ArtifactStatus,
    checks: CheckStatus,
    identity: IdentityBinding,
    rejected: bool,
) -> ExitClass:
    """The single release decision. ``OK`` is reachable only by satisfying every dimension.

    Specific failure classes are tried in a fixed precedence; whatever they do not name falls to
    ``BLOCKED`` rather than ``OK``, so an unforeseen state can never release.
    """
    if envelope_malformed:
        return ExitClass.MALFORMED
    # A well-formed envelope whose signature does not verify is the stronger finding: the body
    # was altered (or never signed by this key), whatever shape it now has.
    if signature is SignatureStatus.INVALID:
        return ExitClass.TAMPERED
    if schema in (SchemaStatus.MALFORMED, SchemaStatus.NON_CANONICAL):
        return ExitClass.MALFORMED
    if schema is SchemaStatus.UNSUPPORTED or signature is SignatureStatus.UNSIGNED:
        return ExitClass.UNSUPPORTED
    if artifacts is ArtifactStatus.MISMATCH:
        return ExitClass.TAMPERED
    acceptable = _trust_acceptable(trust, purpose)
    # An audit reports revocation, retirement and staleness as findings instead of failing on them.
    audit_findings = {TrustStatus.REVOKED, TrustStatus.STALE_POLICY, TrustStatus.RETIRED}
    if not acceptable and not (not purpose.strict and trust in audit_findings):
        return ExitClass.UNTRUSTED
    if artifacts in (ArtifactStatus.MISSING, ArtifactStatus.UNAVAILABLE, ArtifactStatus.UNEXPECTED):
        return ExitClass.INCOMPLETE
    if (
        identity is IdentityBinding.MISMATCH
        or checks in (CheckStatus.FAIL, CheckStatus.INCONCLUSIVE)
        or rejected
    ):
        return ExitClass.BLOCKED
    if (
        not purpose.strict
        or artifacts is ArtifactStatus.NOT_EVALUATED
        or identity is IdentityBinding.NOT_ASSERTED
    ):
        return ExitClass.PARTIAL
    fully_verified = (
        schema is SchemaStatus.SUPPORTED
        and signature is SignatureStatus.VALID
        and acceptable
        and artifacts is ArtifactStatus.MATCH
        and checks is CheckStatus.PASS
        and identity is IdentityBinding.BOUND
    )
    return ExitClass.OK if fully_verified else ExitClass.BLOCKED


def _identity(
    stmt: EvidenceStatementV1 | None, ctx: VerificationContext, reasons: set[str]
) -> IdentityBinding:
    if stmt is None:
        return IdentityBinding.NOT_ASSERTED
    pairs = (
        (ctx.expected_bundle_id, stmt.bundle_id, "IDENTITY_BUNDLE_MISMATCH"),
        (ctx.expected_job_id, stmt.job_id, "IDENTITY_JOB_MISMATCH"),
        (ctx.expected_attempt_id, stmt.attempt_id, "IDENTITY_ATTEMPT_MISMATCH"),
        (ctx.expected_snapshot_id, stmt.source.snapshot_id, "IDENTITY_SNAPSHOT_MISMATCH"),
    )
    asserted = [p for p in pairs if p[0] is not None]
    if not asserted:
        return IdentityBinding.NOT_ASSERTED
    bad = [code for want, got, code in asserted if want != got]
    reasons.update(bad)
    return IdentityBinding.MISMATCH if bad else IdentityBinding.BOUND


def verify_signed_evidence(
    *,
    statement: bytes,
    signature: bytes | None,
    artifact_root: str | os.PathLike[str] | None,
    context: VerificationContext,
    clock: Callable[[], datetime] | None = None,
) -> VerificationResult:
    """Verify a detached-JWS signed evidence statement against trusted expectations.

    ``artifact_root=None`` is a signature-only diagnostic: artifacts are not evaluated and the
    result can never be full success.
    """
    if not isinstance(statement, (bytes, bytearray, memoryview)):
        raise SignedEvidenceError("INPUT_INVALID")
    statement = bytes(statement)
    if signature is not None:
        signature = bytes(signature)
    evaluation_time = context.evaluation_time or (clock or (lambda: datetime.now(timezone.utc)))()
    statement_id = "sha256:" + hashlib.sha256(statement).hexdigest()
    reasons: set[str] = set()
    purpose = context.purpose

    stmt: EvidenceStatementV1 | None = None
    schema = SchemaStatus.MALFORMED
    try:
        stmt, canonical = parse_statement_ex(statement)
        schema = SchemaStatus.SUPPORTED if canonical else SchemaStatus.NON_CANONICAL
        if not canonical:
            reasons.add("STATEMENT_NOT_CANONICAL")
    except SignedEvidenceError as exc:
        schema = _SCHEMA_BY_STATUS.get(exc.status or "", SchemaStatus.MALFORMED)
        reasons.add(exc.code)

    envelope: SignatureEnvelopeV1 | None = None
    key: TrustedKey | None = None
    envelope_malformed = False
    trust = TrustStatus.NOT_EVALUATED
    if signature is None:
        sig_status = SignatureStatus.UNSIGNED
        reasons.add("SIGNATURE_MISSING")
    else:
        try:
            envelope = parse_signature_object(signature)
        except SignedEvidenceError as exc:
            sig_status = SignatureStatus.INVALID
            envelope_malformed = True
            reasons.add(exc.code)
        else:
            key = context.trust_policy.lookup(envelope.kid)
            if key is None:
                sig_status = SignatureStatus.NOT_EVALUATED
                trust = TrustStatus.UNKNOWN_KEY
                reasons.add("KEY_UNKNOWN")
            elif len(statement) > sj.MAX_BYTES:
                # Resource limits come before cryptography: an over-limit body is refused, not signed-over.
                sig_status = SignatureStatus.NOT_EVALUATED
            else:
                ok = _jws.verify_es256_signature(
                    protected_segment=envelope.protected_segment,
                    payload_segment=_jws.b64u_encode(statement),
                    signature_segment=envelope.signature_segment,
                    public_jwk=dict(key.jwk),
                )
                sig_status = SignatureStatus.VALID if ok else SignatureStatus.INVALID
                if not ok:
                    reasons.add("SIGNATURE_INVALID")

    if sig_status is SignatureStatus.VALID and stmt is not None and envelope and key:
        ev = evaluate_trust(
            policy=context.trust_policy,
            key=key,
            header_signer=envelope.decoy_signer,
            header_key_version=envelope.decoy_key_version,
            statement_tenant=stmt.tenant,
            statement_signer=stmt.signer,
            expected_tenant=context.expected_tenant,
            evaluation_time=evaluation_time,
        )
        trust = TrustStatus[ev.status]
        reasons.update(ev.reasons)

    identity = _identity(stmt, context, reasons)

    artifact_results: tuple[ArtifactResult, ...] = ()
    check_results: tuple[CheckResult, ...] = ()
    unbound: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    if stmt is None:
        artifacts, checks = ArtifactStatus.NOT_EVALUATED, CheckStatus.NOT_EVALUATED
    else:
        a_ev = bind_artifacts(
            statement=stmt,
            expected=context.expected_artifacts,
            empty_output_allowed=context.required_catalog.empty_output_allowed,
            root=artifact_root,
        )
        c_ev = evaluate_checks(
            statement=stmt,
            catalog=context.required_catalog,
            expected_artifacts=context.expected_artifacts,
            expected_snapshot_id=context.expected_snapshot_id,
        )
        artifacts, checks = a_ev.status, c_ev.status
        artifact_results, check_results, unbound = a_ev.results, c_ev.results, a_ev.unbound
        reasons.update(a_ev.reasons)
        reasons.update(c_ev.reasons)
        notes = c_ev.notes

    hist_status, hist_ref, hist_reasons = evaluate_historical(
        trust_status=trust.value,
        key=key,
        created_at=parse_utc(stmt.created_at) if stmt else None,
        statement_digest=statement_id,
        signature_digest=envelope.envelope_digest if envelope else "",
        decisions=context.historical_decisions,
    )
    reasons.update(hist_reasons)

    rejected = stmt is not None and stmt.publication.state == "REJECTED"
    if rejected:
        reasons.add("PUBLICATION_STATE_REJECTED")
    if not purpose.strict:
        reasons.add("PURPOSE_HISTORICAL_AUDIT")
    if purpose is Purpose.RELEASE_GATE and trust is TrustStatus.RETIRED:
        reasons.add("SIGNER_NOT_ACTIVE")

    exit_class = _decide(
        purpose=purpose,
        schema=schema,
        signature=sig_status,
        envelope_malformed=envelope_malformed,
        trust=trust,
        artifacts=artifacts,
        checks=checks,
        identity=identity,
        rejected=rejected,
    )
    allow = exit_class is ExitClass.OK

    return VerificationResult(
        statement_id=statement_id,
        schema=schema,
        signature=sig_status,
        trust=trust,
        artifacts=artifacts,
        checks=checks,
        identity=identity,
        release=ReleaseVerdict.ALLOW if allow else ReleaseVerdict.DENY,
        historical_authorization=HistoricalAuthorization[hist_status],
        exit_class=exit_class,
        publication_state=stmt.publication.state if stmt else None,
        purpose=purpose.value,
        time=TimeInfo(stmt.created_at if stmt else None, evaluation_time),
        trust_policy_digest=context.trust_policy.digest,
        catalog_digest=context.required_catalog.digest,
        reasons=tuple(sorted(reasons)),
        notes=notes,
        artifact_results=artifact_results,
        check_results=check_results,
        unbound_files=unbound,
        signer=envelope.decoy_signer if envelope else None,
        key_version=envelope.decoy_key_version if envelope else None,
        historical_evidence_ref=hist_ref,
    )
