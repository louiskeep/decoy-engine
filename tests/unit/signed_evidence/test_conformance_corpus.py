"""Conformance vectors (plan section 2.3): every row of the frozen independent corpus.

The expected results were authored from the plan's contract by the independent generator, so a
mismatch here is a verifier bug or a contract question, never something to "fix" by regenerating.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from decoy_engine.signed_evidence import SignedEvidenceError, verify_signed_evidence
from tests.unit.signed_evidence import _corpus

_DIMENSIONS = {
    "schema": "schema",
    "signature": "signature",
    "trust": "trust",
    "artifacts": "artifacts",
    "checks": "checks",
    "identity": "identity",
    "release": "release",
    "historical": "historical_authorization",
    "exit": "exit_class",
}


def test_corpus_is_not_vacuous() -> None:
    ids = _corpus.vector_ids()
    assert len(ids) >= 120, "an empty or truncated corpus must fail loudly, not pass"
    assert len(ids) == len(set(ids))
    assert _corpus.manifest()["vector_count"] == len(ids)
    for required in (
        "valid_release_jose",
        "valid_release_openssl",
        "a1_flip_csv",
        "a1_flip_parquet",
        "a1_flip_ndjson",
        "p_sig_der",
        "k_required_inconclusive_signed_rejection",
        "k_forged_empty_catalog",
        "t_revoked_created_before_revocation",
        "i_replay_other_attempt",
    ):
        assert required in ids


def test_corpus_files_match_manifest_digests() -> None:
    """The corpus is frozen: any edit to a vector or key changes a digest recorded at generation."""
    m = _corpus.manifest()
    lines = []
    for rel, digest in sorted(m["files"].items()):
        actual = hashlib.sha256((_corpus.CORPUS / rel).read_bytes()).hexdigest()
        assert actual == digest, f"{rel} was modified after the corpus was generated"
        lines.append(f"{rel}:{digest}")
    assert hashlib.sha256("\n".join(lines).encode()).hexdigest() == m["corpus_sha256"]
    on_disk = {
        p.relative_to(_corpus.CORPUS).as_posix()
        for p in _corpus.CORPUS.rglob("*")
        if p.is_file() and "generator" not in p.parts and p.name != "MANIFEST.json"
    }
    assert on_disk == set(m["files"]), "files added to or removed from the frozen corpus"


def test_independent_generation_provenance_is_recorded() -> None:
    gen = _corpus.manifest()["generator"]
    assert gen["language"] == "node"
    assert "canonicalize" in gen["canonicalize"] or "RFC 8785" in gen["canonicalize"]
    assert "jose" in gen and "OpenSSL" in gen["signing"]


@pytest.mark.parametrize("vector_id", _corpus.vector_ids())
def test_vector(vector_id: str, tmp_path: Path) -> None:
    vec = _corpus.load(vector_id)
    exp = vec.expected
    load_error = exp.get("load_error")
    if load_error is not None:
        with pytest.raises(SignedEvidenceError) as ei:
            _corpus.build_context(vec)
        assert ei.value.code == load_error
        return

    context = _corpus.build_context(vec)
    root = (
        None
        if vec.context["signature_only"]
        else _corpus.materialize_artifacts(vec, tmp_path / "a")
    )
    result = verify_signed_evidence(
        statement=vec.statement, signature=vec.signature, artifact_root=root, context=context
    )
    asserted = 0
    for key, attr in _DIMENSIONS.items():
        if key in exp:
            asserted += 1
            assert getattr(result, attr).name == exp[key], (
                f"{vector_id}: {attr} = {getattr(result, attr).name}, want {exp[key]}; "
                f"reasons={result.reasons}"
            )
    assert asserted, "a vector with no asserted dimension would pass vacuously"
    for code in exp.get("reasons_include", []):
        assert code in result.reasons, f"{vector_id}: missing reason {code}; got {result.reasons}"
    for fragment in exp.get("notes_include", []):
        assert any(fragment in n for n in result.notes), f"{vector_id}: missing note {fragment}"
    full_success = exp.get("exit") == "OK"
    assert result.exit_class.is_success is full_success
    assert (exp.get("release") == "ALLOW") is (result.release.name == "ALLOW")


def test_every_non_ok_vector_denies_release() -> None:
    """Cross-cut: an `exit` other than OK must never coexist with release ALLOW in the contract."""
    for vid in _corpus.vector_ids():
        exp = _corpus.load(vid).expected
        if exp.get("exit") not in (None, "OK"):
            assert exp.get("release") != "ALLOW", vid


# ------------------------------------------------------------------ standards


def _standards() -> dict:
    return json.loads((_corpus.CORPUS / "standards.json").read_text(encoding="utf-8"))


def test_rfc7515_a3_primitive_verifies_but_profile_rejects() -> None:
    from decoy_engine.signed_evidence import _jws

    a3 = _standards()["rfc7515_a3_es256"]
    header_b64, payload_b64, sig_b64 = a3["compact"].split(".")
    assert _jws.verify_es256_signature(
        protected_segment=header_b64,
        payload_segment=payload_b64,
        signature_segment=sig_b64,
        public_jwk=a3["public_jwk"],
    ), "the generic ES256 primitive must verify the RFC 7515 A.3 example"
    # The same bytes can never pass the Decoy profile: no typ/cty/crit/signer/key_version.
    from decoy_engine.signed_evidence import _envelope

    with pytest.raises(SignedEvidenceError):
        _envelope.parse_signature_object(
            json.dumps({"protected": header_b64, "signature": sig_b64}).encode()
        )


def test_rfc8785_appendix_b_numbers() -> None:
    from decoy_engine.signed_evidence import _canonical

    for hexbits, expected in _standards()["rfc8785"]["numbers"]:
        value = struct.unpack(">d", bytes.fromhex(hexbits))[0]
        assert _canonical.canonical_bytes(value).decode() == expected, hexbits


def test_rfc8785_utf16_key_order_and_independent_cases() -> None:
    from decoy_engine.signed_evidence import _canonical

    std = _standards()["rfc8785"]
    got = _canonical.canonical_bytes(json.loads(std["sorting"]["input_json"])).decode()
    assert got == std["sorting"]["expected_canonical"]
    # Python's own ordering is by code point and would put the emoji AFTER U+FB33.
    assert sorted(["דּ", "\U0001f600"]) == ["\U0001f600", "דּ"][::-1]
    for case in std["cases"]:
        # JavaScript has only doubles, so the independent implementation read every number as one.
        parsed = json.loads(case["input_json"], parse_int=float)
        assert _canonical.canonical_bytes(parsed).decode() == case["expected"], case["input_json"]
