"""TrustPolicy loading, lifecycle evaluation, historical authorization and context validation."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from decoy_engine.signed_evidence import (
    HistoricalDecision,
    Purpose,
    SignedEvidenceError,
    TrustPolicy,
    VerificationContext,
)
from decoy_engine.signed_evidence import _trust as tr
from tests.unit.signed_evidence import _corpus
from tests.unit.signed_evidence import _factory as f

BASE = json.loads((_corpus.CORPUS / "shared" / "trust-a-active.json").read_text())
T0 = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def _load(doc: dict, pin: str | None = None) -> TrustPolicy:
    raw = json.dumps(doc).encode()
    return TrustPolicy.from_bytes(
        raw, pinned_sha256=pin or "sha256:" + hashlib.sha256(raw).hexdigest()
    )


def _code(doc: dict) -> str:
    with pytest.raises(SignedEvidenceError) as ei:
        _load(doc)
    return ei.value.code


def test_loads_corpus_policy_and_exposes_digest() -> None:
    raw = (_corpus.CORPUS / "shared" / "trust-a-active.json").read_bytes()
    p = TrustPolicy.from_bytes(raw, pinned_sha256="sha256:" + hashlib.sha256(raw).hexdigest())
    assert (
        p.digest == "sha256:" + hashlib.sha256(raw).hexdigest()
        and p.lookup("kv-2026-01") is not None
    )
    assert p.lookup("nope") is None


@pytest.mark.parametrize("pin", ["", "sha256:abc", "SHA256:" + "a" * 64, "a" * 64, None, 5])
def test_pin_must_be_a_well_formed_digest(pin) -> None:
    with pytest.raises(SignedEvidenceError) as ei:
        TrustPolicy.from_bytes(b"{}", pinned_sha256=pin)  # type: ignore[arg-type]
    assert ei.value.code == "TRUST_POLICY_PIN_INVALID"


def test_pin_must_match_the_bytes_exactly() -> None:
    raw = json.dumps(BASE).encode()
    with pytest.raises(SignedEvidenceError) as ei:
        TrustPolicy.from_bytes(
            raw + b" ", pinned_sha256="sha256:" + hashlib.sha256(raw).hexdigest()
        )
    assert ei.value.code == "TRUST_POLICY_DIGEST_MISMATCH"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d.update(extra=1), "TRUST_POLICY_SCHEMA_INVALID"),
        (lambda d: d.update(schema="decoy.trust-policy/v2"), "TRUST_POLICY_SCHEMA_UNSUPPORTED"),
        (lambda d: d.update(keys=[]), "TRUST_POLICY_SCHEMA_INVALID"),
        (lambda d: d.update(expires_at=d["issued_at"]), "TRUST_POLICY_SCHEMA_INVALID"),
        (lambda d: d.update(issued_at="yesterday"), "TRUST_POLICY_SCHEMA_INVALID"),
        (lambda d: d["keys"][0].update(alg="ES384"), "TRUST_POLICY_ALG_UNSUPPORTED"),
        (lambda d: d["keys"][0].update(state="MAYBE"), "TRUST_POLICY_SCHEMA_INVALID"),
        (lambda d: d["keys"][0]["jwk"].update(crv="P-384"), "TRUST_POLICY_KEY_INVALID"),
        (lambda d: d["keys"][0]["jwk"].update(kty="RSA"), "TRUST_POLICY_KEY_INVALID"),
        (lambda d: d["keys"][0]["jwk"].update(d="AAAA"), "TRUST_POLICY_KEY_INVALID"),
        (
            lambda d: d["keys"][0]["jwk"].update(y=d["keys"][0]["jwk"]["x"]),
            "TRUST_POLICY_KEY_INVALID",
        ),
        (lambda d: d["keys"][0].update(jwk_thumbprint="AAAA"), "TRUST_POLICY_THUMBPRINT_MISMATCH"),
        (lambda d: d["keys"][0].update(tenant="has space"), "TRUST_POLICY_SCHEMA_INVALID"),
        (lambda d: d["keys"][0].pop("signer"), "TRUST_POLICY_SCHEMA_INVALID"),
        (
            lambda d: d["keys"][0].update(state="RETIRED"),
            "TRUST_POLICY_LIFECYCLE_INCOHERENT",
        ),  # no retired_at
        (
            lambda d: d["keys"][0].update(state="REVOKED"),
            "TRUST_POLICY_LIFECYCLE_INCOHERENT",
        ),  # no revoked_at
        (
            lambda d: d["keys"][0].update(revoked_at="2026-10-01T00:00:00Z"),
            "TRUST_POLICY_LIFECYCLE_INCOHERENT",
        ),  # ACTIVE + revoked
        (
            lambda d: d["keys"][0].update(compromised_since="2026-10-01T00:00:00Z"),
            "TRUST_POLICY_LIFECYCLE_INCOHERENT",
        ),
    ],
)
def test_policy_schema_and_lifecycle_rules(mutate, code) -> None:
    d = copy.deepcopy(BASE)
    mutate(d)
    assert _code(d) == code


def test_duplicate_and_conflicting_keys_are_refused() -> None:
    d = copy.deepcopy(BASE)
    k = copy.deepcopy(BASE["keys"][0])
    dup_kid = {**copy.deepcopy(k), "key_version": "other"}
    d["keys"] = [k, dup_kid]
    assert _code(d) == "TRUST_POLICY_DUPLICATE_KID"
    same_thumb = {**copy.deepcopy(k), "kid": "kv-other", "key_version": "other"}
    d["keys"] = [k, same_thumb]
    assert _code(d) == "TRUST_POLICY_KEY_CONFLICT", "one public key under two identities"


# ------------------------------------------------------------------ evaluate_trust


def _eval(key_over=None, **kw):
    p = _load(BASE)
    key = p.lookup("kv-2026-01")
    assert key is not None
    if key_over:
        import dataclasses

        key = dataclasses.replace(key, **key_over)
    args = {
        "policy": p,
        "key": key,
        "header_signer": key.signer,
        "header_key_version": key.key_version,
        "statement_tenant": key.tenant,
        "statement_signer": key.signer,
        "expected_tenant": key.tenant,
        "evaluation_time": T0,
    }
    args.update(kw)
    return tr.evaluate_trust(**args)


def test_trusted_baseline() -> None:
    assert _eval().status == "TRUSTED"


def test_policy_expiry_boundary_is_exclusive_of_the_expiry_instant() -> None:
    p = _load(BASE)
    assert _eval(evaluation_time=p.expires_at).status == "STALE_POLICY"
    assert (
        _eval(evaluation_time=datetime(2027, 3, 31, 23, 59, 59, tzinfo=timezone.utc)).status
        == "TRUSTED"
    )
    assert _eval(evaluation_time=datetime(2026, 9, 30, tzinfo=timezone.utc)).reasons == (
        "TRUST_POLICY_NOT_YET_VALID",
    )


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"header_signer": "decoy:other"}, "SIGNER_MISMATCH"),
        ({"statement_signer": "decoy:other"}, "SIGNER_MISMATCH"),
        ({"header_key_version": "9999"}, "KEY_VERSION_MISMATCH"),
        ({"statement_tenant": "tenant-other"}, "TENANT_MISMATCH"),
        ({"expected_tenant": "tenant-other"}, "TENANT_MISMATCH"),
    ],
)
def test_scope_mismatches_are_reported_individually(over, reason) -> None:
    ev = _eval(**over)
    assert ev.status == "SCOPE_MISMATCH" and reason in ev.reasons


def test_key_tenant_must_equal_statement_tenant_even_without_an_expectation() -> None:
    # The key is pinned to another tenant while the statement (and header) claim tenant-acme.
    ev = _eval(
        key_over={"tenant": "tenant-other"}, statement_tenant="tenant-acme", expected_tenant=None
    )
    assert ev.status == "SCOPE_MISMATCH" and "TENANT_MISMATCH" in ev.reasons


def test_lifecycle_states() -> None:
    assert _eval(key_over={"state": tr.KeyState.RETIRED}).status == "RETIRED"
    assert _eval(key_over={"state": tr.KeyState.REVOKED}).status == "REVOKED"


def test_scope_is_checked_before_lifecycle_so_a_revoked_key_cannot_hide_a_mismatch() -> None:
    assert (
        _eval(key_over={"state": tr.KeyState.REVOKED}, header_key_version="x").status
        == "SCOPE_MISMATCH"
    )


# ------------------------------------------------------------------ historical


def _decision(stmt="sha256:" + "1" * 64, sig="sha256:" + "2" * 64):
    return HistoricalDecision(
        statement_sha256=stmt,
        signature_sha256=sig,
        decision_ref="archive://d1",
        policy_digest="sha256:" + "3" * 64,
    )


def _hist(trust="REVOKED", created="2026-10-10T00:00:00Z", **kw):
    p = _load(BASE)
    key = p.lookup("kv-2026-01")
    import dataclasses

    key = dataclasses.replace(
        key,
        state=tr.KeyState.REVOKED,
        revoked_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
        **kw.pop("key_over", {}),
    )
    args = {
        "trust_status": trust,
        "key": key,
        "created_at": tr.parse_utc(created),
        "statement_digest": "sha256:" + "1" * 64,
        "signature_digest": "sha256:" + "2" * 64,
        "decisions": (),
    }
    args.update(kw)
    return tr.evaluate_historical(**args)


def test_historical_is_not_applicable_unless_revoked_or_stale() -> None:
    for status in ("TRUSTED", "RETIRED", "UNKNOWN_KEY", "SCOPE_MISMATCH", "NOT_EVALUATED"):
        assert _hist(trust=status)[0] == "NOT_APPLICABLE"


def test_backdating_cannot_grant_but_a_claim_of_signing_after_revocation_refuses() -> None:
    assert _hist(created="2026-10-10T00:00:00Z")[0] == "UNKNOWN"
    assert _hist(created="2026-10-20T00:00:00Z")[0] == "DISALLOWED"
    assert _hist(created="2026-10-25T00:00:00Z")[0] == "DISALLOWED"


def test_compromise_window_refuses_claims_inside_it() -> None:
    over = {"compromised_since": datetime(2026, 10, 5, tzinfo=timezone.utc)}
    assert _hist(created="2026-10-06T00:00:00Z", key_over=over)[0] == "DISALLOWED"
    assert _hist(created="2026-10-04T00:00:00Z", key_over=over)[0] == "UNKNOWN"


def test_decision_must_bind_both_statement_and_signature() -> None:
    assert _hist(decisions=(_decision(),))[0:2] == ("ESTABLISHED", "archive://d1")
    assert _hist(decisions=(_decision(stmt="sha256:" + "9" * 64),))[0] == "UNKNOWN"
    assert _hist(decisions=(_decision(sig="sha256:" + "9" * 64),))[0] == "UNKNOWN"


def test_established_beats_disallowed_when_an_external_decision_binds_the_bundle() -> None:
    assert _hist(created="2026-10-25T00:00:00Z", decisions=(_decision(),))[0] == "ESTABLISHED"


def test_stale_policy_history_is_unknown_without_a_decision() -> None:
    assert _hist(trust="STALE_POLICY")[0] == "UNKNOWN"
    assert _hist(trust="STALE_POLICY", decisions=(_decision(),))[0] == "ESTABLISHED"


def test_decision_value_validation() -> None:
    for kw in ({"stmt": "bad"}, {"sig": "bad"}):
        with pytest.raises(SignedEvidenceError):
            _decision(**kw)
    with pytest.raises(SignedEvidenceError):
        HistoricalDecision(
            statement_sha256="sha256:" + "1" * 64,
            signature_sha256="sha256:" + "2" * 64,
            decision_ref="",
            policy_digest="sha256:" + "3" * 64,
        )


# ------------------------------------------------------------------ context


def _ctx(**over):
    s = f.new_signer()
    base = {
        "purpose": Purpose.RELEASE_GATE,
        "trust_policy": f.policy_for(s),
        "required_catalog": f.catalog(),
        "expected_tenant": "tenant-acme",
        "expected_job_id": "job-1",
        "expected_attempt_id": "a-1",
        "expected_snapshot_id": "snap-1",
        "evaluation_time": T0,
    }
    base.update(over)
    return VerificationContext(**base)


@pytest.mark.parametrize(
    "missing", ["expected_tenant", "expected_job_id", "expected_attempt_id", "expected_snapshot_id"]
)
@pytest.mark.parametrize("purpose", [Purpose.RELEASE_GATE, Purpose.CONSUMER_ACCEPTANCE])
def test_strict_purposes_require_the_identity_expectations(missing, purpose) -> None:
    with pytest.raises(SignedEvidenceError) as ei:
        _ctx(purpose=purpose, **{missing: None})
    assert ei.value.code == "CONTEXT_INCOMPLETE"


def test_historical_audit_needs_no_expectations() -> None:
    c = _ctx(
        purpose=Purpose.HISTORICAL_AUDIT,
        expected_tenant=None,
        expected_job_id=None,
        expected_attempt_id=None,
        expected_snapshot_id=None,
    )
    assert not c.purpose.strict


@pytest.mark.parametrize(
    "over",
    [
        {"expected_job_id": "has space"},
        {"expected_tenant": ""},
        {"purpose": "RELEASE_GATE"},
        {"trust_policy": object()},
        {"required_catalog": object()},
        {"evaluation_time": datetime(2026, 10, 29)},  # naive
    ],
)
def test_context_values_are_validated(over) -> None:
    with pytest.raises(SignedEvidenceError) as ei:
        _ctx(**over)
    assert ei.value.code == "CONTEXT_INVALID"


def test_duplicate_expected_artifact_ids_are_refused() -> None:
    a = f.expected_artifacts()
    with pytest.raises(SignedEvidenceError):
        _ctx(expected_artifacts=(a[0], a[0]))


def test_context_is_frozen() -> None:
    c = _ctx()
    with pytest.raises(AttributeError):
        c.expected_job_id = "x"  # type: ignore[misc]
