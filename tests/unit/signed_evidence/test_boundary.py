"""The signing boundary: the only constructor of a signed carrier (A7, A11, A12 at the core)."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from decoy_engine.signed_evidence import (
    ExitClass,
    LocalEs256Signer,
    Purpose,
    SignedEvidenceError,
    SigningIdentity,
    verify_signed_evidence,
)
from decoy_engine.signed_evidence import _boundary as boundary
from decoy_engine.signed_evidence import _release as release
from tests.unit.signed_evidence import _factory as f


@pytest.fixture
def signer() -> LocalEs256Signer:
    return f.new_signer()


@pytest.fixture
def policy(signer: LocalEs256Signer):
    return f.policy_for(signer)


def _verify(signed, policy, tmp_path, purpose=Purpose.RELEASE_GATE):
    root = f.write_artifacts(tmp_path / "art")
    return verify_signed_evidence(
        statement=signed.statement,
        signature=signed.signature,
        artifact_root=root,
        context=f.context(policy, purpose=purpose),
    )


def test_signed_evidence_verifies_end_to_end(signer, policy, tmp_path) -> None:
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    r = _verify(signed, policy, tmp_path)
    assert (
        r.exit_class is ExitClass.OK and r.signature.name == "VALID" and r.release.name == "ALLOW"
    )
    assert signed.statement_id == "sha256:" + hashlib.sha256(signed.statement).hexdigest()
    assert (signed.signer, signed.key_version) == ("decoy:acme-prod", "2026-01")


def test_statement_is_canonical_and_stable_but_signatures_are_not_required_to_be(signer) -> None:
    a = boundary.sign_evidence(f.base_draft(), signer=signer)
    b = boundary.sign_evidence(f.base_draft(), signer=signer)
    assert a.statement == b.statement and a.statement_id == b.statement_id


def test_mutating_the_source_dict_after_signing_cannot_change_emitted_bytes(signer) -> None:
    draft = f.base_draft()
    signed = boundary.sign_evidence(draft, signer=signer)
    before = (signed.statement, signed.signature)
    draft["job_id"] = "attacker-job"
    draft["checks"][0]["status"] = "FAIL"
    draft["artifacts"].clear()
    assert (signed.statement, signed.signature) == before
    assert json.loads(signed.statement)["job_id"] == "job-1001"


def test_carrier_is_immutable_and_not_publicly_constructible(signer) -> None:
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    with pytest.raises(AttributeError):
        signed.statement = b"x"  # type: ignore[misc]
    with pytest.raises(SignedEvidenceError) as ei:
        boundary.SignedEvidence(statement=signed.statement, signature=signed.signature)  # type: ignore[call-arg]
    assert ei.value.code == "CARRIER_CONSTRUCTION_FORBIDDEN"
    assert isinstance(signed.statement, bytes) and isinstance(signed.signature, bytes)


def test_signing_refuses_an_invalid_draft_before_touching_the_signer() -> None:
    class Tripwire:
        identity = SigningIdentity("decoy:acme-prod", "kv", "v1")

        def public_jwk(self):  # pragma: no cover - must not be reached
            raise AssertionError("signer consulted for an invalid draft")

        def sign_signing_input(self, _):  # pragma: no cover
            raise AssertionError("signer consulted for an invalid draft")

    bad = f.base_draft()
    bad["extra"] = 1
    with pytest.raises(SignedEvidenceError) as ei:
        boundary.sign_evidence(bad, signer=Tripwire())
    assert ei.value.code == "STATEMENT_UNKNOWN_FIELD"


def test_issuer_signer_must_equal_the_resolved_identity(signer) -> None:
    with pytest.raises(SignedEvidenceError) as ei:
        boundary.sign_evidence(f.base_draft(signer="decoy:somebody-else"), signer=signer)
    assert ei.value.code == "SIGNER_IDENTITY_MISMATCH"


class _Wrapper:
    """Wraps a real signer to inject provider misbehaviour."""

    def __init__(self, inner: LocalEs256Signer, sign=None, jwk=None, identity=None) -> None:
        self._inner, self._sign, self._jwk, self._identity = inner, sign, jwk, identity

    @property
    def identity(self):
        return self._identity or self._inner.identity

    def public_jwk(self):
        return self._jwk or self._inner.public_jwk()

    def sign_signing_input(self, signing_input: bytes) -> bytes:
        return (self._sign or self._inner.sign_signing_input)(signing_input)


def _expect(signer, code: str) -> None:
    with pytest.raises(SignedEvidenceError) as ei:
        boundary.sign_evidence(f.base_draft(), signer=signer)
    assert ei.value.code == code, ei.value.code


def test_provider_failures_yield_no_carrier_and_leak_no_provider_text(signer) -> None:
    secret = "arn:aws:kms:eu-west-1:123456789012:key/SECRET-PROVIDER-DETAIL"

    def deny(_):
        raise PermissionError(secret)

    def timeout(_):
        raise TimeoutError(secret)

    for fn in (deny, timeout):
        with pytest.raises(SignedEvidenceError) as ei:
            boundary.sign_evidence(f.base_draft(), signer=_Wrapper(signer, sign=fn))
        assert ei.value.code == "SIGNING_FAILED" and secret not in str(ei.value) + repr(ei.value)


def test_malformed_provider_signatures_are_rejected(signer) -> None:
    good = signer.sign_signing_input
    _expect(_Wrapper(signer, sign=lambda m: good(m)[:63]), "SIGNER_BAD_SIGNATURE")  # truncated
    _expect(_Wrapper(signer, sign=lambda m: good(m) + b"\x00"), "SIGNER_BAD_SIGNATURE")  # over-long
    _expect(_Wrapper(signer, sign=lambda m: b""), "SIGNER_BAD_SIGNATURE")
    _expect(_Wrapper(signer, sign=lambda m: "x" * 64), "SIGNER_BAD_SIGNATURE")  # type: ignore[arg-type, return-value]
    flipped = lambda m: bytes([good(m)[0] ^ 1]) + good(m)[1:]
    _expect(_Wrapper(signer, sign=flipped), "SIGNER_BAD_SIGNATURE")  # does not verify


def test_der_output_from_a_provider_is_converted_not_trusted(signer) -> None:
    """A KMS that returns DER must be adapted with der_to_p1363; raw DER is refused."""
    key = ec.generate_private_key(ec.SECP256R1())

    class DerSigner(_Wrapper):
        pass

    der_signer = DerSigner(
        signer,
        sign=lambda m: key.sign(m, ec.ECDSA(hashes.SHA256())),  # 70-72 bytes
        jwk=f.LocalEs256Signer(key, signer.identity).public_jwk(),
    )
    _expect(der_signer, "SIGNER_BAD_SIGNATURE")


def test_wrong_key_or_curve_from_the_provider_is_rejected(signer) -> None:
    other = f.new_signer(kid="kv-other")
    # signs with a different key than the public key it advertises
    _expect(_Wrapper(signer, sign=other.sign_signing_input), "SIGNER_BAD_SIGNATURE")
    # advertises a P-384 public key
    p384 = ec.generate_private_key(ec.SECP384R1()).public_key()
    from cryptography.hazmat.primitives import serialization
    from joserfc.jwk import ECKey

    jwk = ECKey.import_key(
        p384.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).as_dict(private=False)
    _expect(
        _Wrapper(signer, jwk={k: jwk[k] for k in ("kty", "crv", "x", "y")}), "SIGNER_BAD_SIGNATURE"
    )


def test_double_hashing_is_caught_by_local_verification(signer) -> None:
    """Signing sha256(input) as if it were the message yields a signature over the wrong bytes."""
    key = ec.generate_private_key(ec.SECP256R1())
    inner = f.LocalEs256Signer(key, signer.identity)
    _expect(
        _Wrapper(inner, sign=lambda m: inner.sign_signing_input(hashlib.sha256(m).digest())),
        "SIGNER_BAD_SIGNATURE",
    )


def test_identity_is_resolved_exactly_once(signer, policy, tmp_path) -> None:
    """An alias that rotates mid-flight must not change the header the signature covers."""
    calls: list[int] = []

    class Rotating(_Wrapper):
        @property
        def identity(self):
            calls.append(1)
            return (
                SigningIdentity("decoy:acme-prod", "kv-test-1", "2026-01")
                if len(calls) == 1
                else SigningIdentity("decoy:acme-prod", "kv-OTHER", "9999")
            )

    signed = boundary.sign_evidence(f.base_draft(), signer=Rotating(signer))
    assert len(calls) == 1
    assert _verify(signed, policy, tmp_path).exit_class is ExitClass.OK


def test_signed_rejection_is_possible_and_never_yields_a_permit(signer, policy, tmp_path) -> None:
    draft = f.base_draft()
    draft["publication"] = {"state": "REJECTED", "reason_code": "REQUIRED_CHECK_INCONCLUSIVE"}
    for a in draft["artifacts"]:
        a["disposition"] = "WITHHELD"
    draft["checks"][1]["status"] = "INCONCLUSIVE"
    draft["checks"][1]["reason_code"] = "DETECTOR_UNAVAILABLE"
    signed = boundary.sign_evidence(draft, signer=signer)
    assert signed.publication_state == "REJECTED"
    decision = release.StrictReleaseEvaluator().evaluate(
        signed, artifact_root=None, context=f.context(policy), policy_generation="g1"
    )
    assert decision.permit is None and "PUBLICATION_STATE_REJECTED" in decision.reasons


def test_default_release_evaluator_denies_even_a_perfect_bundle(signer, policy, tmp_path) -> None:
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    root = f.write_artifacts(tmp_path / "art")
    ctx = f.context(policy)
    strict = release.StrictReleaseEvaluator().evaluate(
        signed, artifact_root=root, context=ctx, policy_generation="g1"
    )
    assert strict.permit is not None
    deny = release.DenyAllReleaseEvaluator().evaluate(
        signed, artifact_root=root, context=ctx, policy_generation="g1"
    )
    assert deny.permit is None and deny.reasons == ("RELEASE_EVALUATOR_NOT_ENABLED",)


def test_permit_binds_statement_inventory_attempt_and_generation(signer, policy, tmp_path) -> None:
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    root = f.write_artifacts(tmp_path / "art")
    permit = (
        release.StrictReleaseEvaluator()
        .evaluate(signed, artifact_root=root, context=f.context(policy), policy_generation="gen-7")
        .permit
    )
    assert permit is not None
    assert permit.statement_id == signed.statement_id
    assert permit.attempt_id == "attempt-1" and permit.policy_generation == "gen-7"
    inventory = json.loads(signed.statement)["artifacts"]
    assert (
        permit.inventory_digest
        == "sha256:"
        + hashlib.sha256(
            json.dumps(
                inventory, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
    )
    with pytest.raises(SignedEvidenceError) as ei:
        release.ReleasePermit(  # type: ignore[call-arg]
            statement_id=permit.statement_id,
            inventory_digest="x",
            attempt_id="a",
            policy_generation="g",
        )
    assert ei.value.code == "PERMIT_CONSTRUCTION_FORBIDDEN"


def test_strict_evaluator_refuses_non_release_gate_purposes(signer, policy, tmp_path) -> None:
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    root = f.write_artifacts(tmp_path / "art")
    for purpose in (Purpose.CONSUMER_ACCEPTANCE, Purpose.HISTORICAL_AUDIT):
        d = release.StrictReleaseEvaluator().evaluate(
            signed,
            artifact_root=root,
            context=f.context(policy, purpose=purpose),
            policy_generation="g",
        )
        assert d.permit is None and "PURPOSE_NOT_RELEASE_GATE" in d.reasons


def test_strict_evaluator_denies_on_any_failed_dimension(signer, policy, tmp_path) -> None:
    root = f.write_artifacts(tmp_path / "art")
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    ev = release.StrictReleaseEvaluator()
    # tampered artifact
    (root / "outputs/customers.csv").write_bytes(b"id,name\n1,Mallory\n3,Chloe\n2,Bob\n")
    assert (
        ev.evaluate(
            signed, artifact_root=root, context=f.context(policy), policy_generation="g"
        ).permit
        is None
    )
    # untrusted signer
    root2 = f.write_artifacts(tmp_path / "art2")
    stranger_policy = f.policy_for(f.new_signer(kid="kv-someone-else"))
    d = ev.evaluate(
        signed, artifact_root=root2, context=f.context(stranger_policy), policy_generation="g"
    )
    assert d.permit is None and "KEY_UNKNOWN" in d.reasons


def test_adopting_existing_evidence_preserves_bytes_and_original_signer(signer, policy) -> None:
    original = boundary.sign_evidence(f.base_draft(), signer=signer)
    adopted = boundary.adopt_verified_evidence(
        statement=original.statement,
        signature=original.signature,
        context=f.context(policy, purpose=Purpose.CONSUMER_ACCEPTANCE),
    )
    assert (adopted.statement, adopted.signature) == (original.statement, original.signature)
    assert (adopted.signer, adopted.key_version) == (original.signer, original.key_version)


def test_adopting_refuses_unverified_evidence(signer, policy) -> None:
    original = boundary.sign_evidence(f.base_draft(), signer=signer)
    ctx = f.context(policy, purpose=Purpose.CONSUMER_ACCEPTANCE)
    tampered = bytearray(original.statement)
    tampered[10] ^= 1
    for statement, signature, code in (
        (bytes(tampered), original.signature, "ADOPT_NOT_VERIFIED"),
        (original.statement, None, "ADOPT_NOT_VERIFIED"),
        (original.statement, original.signature[:-5], "ADOPT_NOT_VERIFIED"),
    ):
        with pytest.raises(SignedEvidenceError) as ei:
            boundary.adopt_verified_evidence(statement=statement, signature=signature, context=ctx)  # type: ignore[arg-type]
        assert ei.value.code == code
    revoked = f.policy_for(
        signer,
        states={signer.identity.kid: {"state": "REVOKED", "revoked_at": "2026-10-20T00:00:00Z"}},
    )
    with pytest.raises(SignedEvidenceError):
        boundary.adopt_verified_evidence(
            statement=original.statement,
            signature=original.signature,
            context=f.context(revoked, purpose=Purpose.CONSUMER_ACCEPTANCE),
        )


def test_no_public_api_accepts_a_boolean_signature_verdict() -> None:
    """A persisted carrier cannot be manufactured from a `signature_valid=True` style flag."""
    for fn in (
        boundary.sign_evidence,
        boundary.adopt_verified_evidence,
        release.StrictReleaseEvaluator.evaluate,
    ):
        flagged = [n for n in inspect.signature(fn).parameters if "valid" in n or "verdict" in n]
        assert not flagged, (fn.__qualname__, flagged)


def test_signature_object_is_flattened_detached_with_exactly_two_members(signer) -> None:
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    obj = json.loads(signed.signature)
    assert set(obj) == {"protected", "signature"}
    copy.deepcopy(obj)
    payload_hdr: Any = json.loads(__import__("base64").urlsafe_b64decode(obj["protected"] + "=="))
    assert payload_hdr["alg"] == "ES256" and payload_hdr["crit"] == [
        "decoy_signer",
        "decoy_key_version",
    ]
    assert payload_hdr["typ"].endswith("evidence-signature.v1+jws")


# ---- persisted carriers are revalidated, never unpickled into authority ---------------------------


def test_carriers_and_permits_cannot_be_pickled_into_existence(signer, policy, tmp_path) -> None:
    import pickle

    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    with pytest.raises(TypeError):
        pickle.dumps(signed)
    root = f.write_artifacts(tmp_path / "art")
    permit = (
        release.StrictReleaseEvaluator()
        .evaluate(signed, artifact_root=root, context=f.context(policy), policy_generation="g")
        .permit
    )
    assert permit is not None
    with pytest.raises(TypeError):
        pickle.dumps(permit)


def test_copying_an_immutable_carrier_is_allowed_and_yields_the_same_value(signer) -> None:
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    assert copy.copy(signed) is signed and copy.deepcopy(signed) is signed


# ---- resource limits come before crypto ------------------------------------------------------------


def test_oversized_statement_is_malformed_without_attempting_a_signature(
    signer, policy, monkeypatch
) -> None:
    from decoy_engine.signed_evidence import _jws
    from decoy_engine.signed_evidence import _strict_json as sj

    signed = boundary.sign_evidence(
        f.base_draft(), signer=signer
    )  # its own local verify is not under test
    calls: list[int] = []
    real = _jws.verify_es256_signature
    monkeypatch.setattr(_jws, "verify_es256_signature", lambda **kw: calls.append(1) or real(**kw))
    r = verify_signed_evidence(
        statement=b" " * (sj.MAX_BYTES + 1),
        signature=signed.signature,
        artifact_root=None,
        context=f.context(policy),
    )
    assert r.exit_class is ExitClass.MALFORMED and "STATEMENT_TOO_LARGE" in r.reasons
    assert r.signature.name == "NOT_EVALUATED"
    assert calls == [], "no cryptography may run on an over-limit statement"
    # control: the same spy does fire for an in-limit statement, so the assertion above is not vacuous
    verify_signed_evidence(
        statement=signed.statement,
        signature=signed.signature,
        artifact_root=None,
        context=f.context(policy),
    )
    assert calls == [1]


def test_adoption_refuses_evidence_for_a_different_job_or_attempt(signer, policy) -> None:
    original = boundary.sign_evidence(f.base_draft(), signer=signer)
    for over in (
        {"expected_job_id": "job-OTHER"},
        {"expected_attempt_id": "attempt-OTHER"},
        {"expected_bundle_id": "bundle-OTHER"},
    ):
        ctx = f.context(policy, purpose=Purpose.CONSUMER_ACCEPTANCE, **over)
        with pytest.raises(SignedEvidenceError) as ei:
            boundary.adopt_verified_evidence(
                statement=original.statement, signature=original.signature, context=ctx
            )
        assert ei.value.code == "ADOPT_NOT_VERIFIED", over


def test_adoption_refuses_a_valid_signature_from_an_unknown_key(signer) -> None:
    original = boundary.sign_evidence(f.base_draft(), signer=signer)
    stranger = f.policy_for(f.new_signer(kid="kv-someone-else"))
    with pytest.raises(SignedEvidenceError):
        boundary.adopt_verified_evidence(
            statement=original.statement,
            signature=original.signature,
            context=f.context(stranger, purpose=Purpose.CONSUMER_ACCEPTANCE),
        )


def test_adoption_refuses_a_validly_signed_but_non_canonical_statement(signer, policy) -> None:
    """The signature here is VALID over the pretty-printed bytes; only canonicality can refuse it."""
    import json as _json

    from decoy_engine.signed_evidence import _envelope, _jws

    pretty = _json.dumps(
        _json.loads(boundary.sign_evidence(f.base_draft(), signer=signer).statement), indent=2
    ).encode()
    header = {
        "alg": "ES256",
        "kid": signer.identity.kid,
        "typ": _envelope.TYP,
        "cty": _envelope.CTY,
        "crit": ["decoy_signer", "decoy_key_version"],
        "decoy_signer": signer.identity.signer,
        "decoy_key_version": signer.identity.key_version,
    }
    protected, sig = _jws.sign_detached(protected_header=header, statement=pretty, signer=signer)
    signature = _json.dumps({"protected": protected, "signature": sig}).encode()
    ctx = f.context(policy, purpose=Purpose.CONSUMER_ACCEPTANCE)
    r = verify_signed_evidence(
        statement=pretty, signature=signature, artifact_root=None, context=ctx
    )
    assert r.signature.name == "VALID" and r.schema.name == "NON_CANONICAL", (
        "premise: authentic but non-canonical"
    )
    with pytest.raises(SignedEvidenceError) as ei:
        boundary.adopt_verified_evidence(statement=pretty, signature=signature, context=ctx)
    assert ei.value.code == "ADOPT_NOT_VERIFIED"


def test_a_permit_requires_a_policy_generation(signer, policy, tmp_path) -> None:
    """The generation is what a later recheck compares; an empty one would make the recheck vacuous."""
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    root = f.write_artifacts(tmp_path / "art")
    for generation in ("", None):
        d = release.StrictReleaseEvaluator().evaluate(
            signed,
            artifact_root=root,
            context=f.context(policy),
            policy_generation=generation,  # type: ignore[arg-type]
        )
        assert d.permit is None and "POLICY_GENERATION_MISSING" in d.reasons


def test_a_permit_is_refused_when_the_carrier_and_context_disagree_on_the_attempt(
    signer, policy, tmp_path
) -> None:
    signed = boundary.sign_evidence(f.base_draft(), signer=signer)
    root = f.write_artifacts(tmp_path / "art")
    d = release.StrictReleaseEvaluator().evaluate(
        signed,
        artifact_root=root,
        context=f.context(policy, expected_attempt_id="attempt-OTHER"),
        policy_generation="g",
    )
    assert d.permit is None
