"""The release decision, checked exhaustively against the plan's strict-eligibility conjunction.

`_decide` is the single place `ExitClass.OK` (and therefore `release == ALLOW`) is produced. The
oracle below restates plan 1.7 independently; every combination of every dimension must agree.
"""

from __future__ import annotations

import itertools

import pytest

from decoy_engine.signed_evidence import (
    ArtifactStatus,
    CheckStatus,
    ExitClass,
    IdentityBinding,
    Purpose,
    SchemaStatus,
    SignatureStatus,
    TrustStatus,
)
from decoy_engine.signed_evidence._verifier import _decide


def _oracle_ok(
    purpose, schema, signature, malformed, trust, artifacts, checks, identity, rejected
) -> bool:
    """Plan 1.7: strict eligibility is the conjunction of every dimension; nothing else releases."""
    trust_ok = trust is TrustStatus.TRUSTED or (
        trust is TrustStatus.RETIRED and purpose is Purpose.CONSUMER_ACCEPTANCE
    )
    return (
        purpose in (Purpose.RELEASE_GATE, Purpose.CONSUMER_ACCEPTANCE)
        and not malformed
        and schema is SchemaStatus.SUPPORTED
        and signature is SignatureStatus.VALID
        and trust_ok
        and artifacts is ArtifactStatus.MATCH
        and checks is CheckStatus.PASS
        and identity is IdentityBinding.BOUND
        and not rejected
    )


_DIMS = (
    list(Purpose),
    list(SchemaStatus),
    list(SignatureStatus),
    [False, True],
    list(TrustStatus),
    list(ArtifactStatus),
    list(CheckStatus),
    list(IdentityBinding),
    [False, True],
)


def test_ok_is_produced_exactly_when_every_dimension_is_satisfied() -> None:
    combos = 0
    oks = 0
    for purpose, schema, sig, malformed, trust, art, chk, ident, rej in itertools.product(*_DIMS):
        combos += 1
        got = _decide(
            purpose=purpose,
            schema=schema,
            signature=sig,
            envelope_malformed=malformed,
            trust=trust,
            artifacts=art,
            checks=chk,
            identity=ident,
            rejected=rej,
        )
        want = _oracle_ok(purpose, schema, sig, malformed, trust, art, chk, ident, rej)
        assert (got is ExitClass.OK) is want, (
            purpose,
            schema,
            sig,
            malformed,
            trust,
            art,
            chk,
            ident,
            rej,
            got,
        )
        oks += want
    assert combos > 50_000, "the exhaustive product must really run"
    assert oks > 0, "the oracle must admit at least one combination or the test is vacuous"


def test_a_release_gate_never_accepts_a_retired_signer_but_consumer_acceptance_does() -> None:
    base = {
        "schema": SchemaStatus.SUPPORTED,
        "signature": SignatureStatus.VALID,
        "envelope_malformed": False,
        "trust": TrustStatus.RETIRED,
        "artifacts": ArtifactStatus.MATCH,
        "checks": CheckStatus.PASS,
        "identity": IdentityBinding.BOUND,
        "rejected": False,
    }
    assert _decide(purpose=Purpose.RELEASE_GATE, **base) is ExitClass.UNTRUSTED
    assert _decide(purpose=Purpose.CONSUMER_ACCEPTANCE, **base) is ExitClass.OK
    assert _decide(purpose=Purpose.HISTORICAL_AUDIT, **base) is ExitClass.PARTIAL


@pytest.mark.parametrize(
    "trust", [TrustStatus.REVOKED, TrustStatus.STALE_POLICY, TrustStatus.RETIRED]
)
def test_audit_reports_lifecycle_findings_without_failing_on_them(trust) -> None:
    got = _decide(
        purpose=Purpose.HISTORICAL_AUDIT,
        schema=SchemaStatus.SUPPORTED,
        signature=SignatureStatus.VALID,
        envelope_malformed=False,
        trust=trust,
        artifacts=ArtifactStatus.MATCH,
        checks=CheckStatus.PASS,
        identity=IdentityBinding.BOUND,
        rejected=False,
    )
    assert got is ExitClass.PARTIAL


@pytest.mark.parametrize("trust", [TrustStatus.UNKNOWN_KEY, TrustStatus.SCOPE_MISMATCH])
def test_audit_still_fails_on_an_unknown_or_out_of_scope_signer(trust) -> None:
    got = _decide(
        purpose=Purpose.HISTORICAL_AUDIT,
        schema=SchemaStatus.SUPPORTED,
        signature=SignatureStatus.VALID,
        envelope_malformed=False,
        trust=trust,
        artifacts=ArtifactStatus.MATCH,
        checks=CheckStatus.PASS,
        identity=IdentityBinding.BOUND,
        rejected=False,
    )
    assert got is ExitClass.UNTRUSTED


def test_a_broken_signature_is_tampering_even_when_the_body_is_also_malformed() -> None:
    got = _decide(
        purpose=Purpose.RELEASE_GATE,
        schema=SchemaStatus.MALFORMED,
        signature=SignatureStatus.INVALID,
        envelope_malformed=False,
        trust=TrustStatus.NOT_EVALUATED,
        artifacts=ArtifactStatus.NOT_EVALUATED,
        checks=CheckStatus.NOT_EVALUATED,
        identity=IdentityBinding.NOT_ASSERTED,
        rejected=False,
    )
    assert got is ExitClass.TAMPERED
