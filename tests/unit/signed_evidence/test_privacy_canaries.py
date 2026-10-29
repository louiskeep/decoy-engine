"""A11: seeded canaries never surface in results, errors, reprs or logs."""

from __future__ import annotations

import json
import logging
import re

import pytest
import rfc8785

from decoy_engine.signed_evidence import SignedEvidenceError, verify_signed_evidence
from decoy_engine.signed_evidence import _statement as st
from tests.unit.signed_evidence import _corpus

CANARIES = [
    "CANARY-SOURCE-VALUE-alice@example.com",
    "CANARY-SECRET-REF-vault://prod/db-password",
    r"CANARY-REGEX-(?P<ssn>\d{3}-\d{2}-\d{4})",
    "CANARY-EXC-KeyError('customers.ssn')",
    "CANARY-SAMPLE-4111111111111111",
]


def _surfaces(r) -> str:
    return " ".join([json.dumps(r.to_dict(), default=str), repr(r), str(r), *r.reasons, *r.notes])


def _all_check_text(r) -> str:
    return json.dumps([c.to_dict() for c in r.check_results], default=str)


@pytest.mark.parametrize("canary", CANARIES)
def test_artifact_content_and_malformed_fields_never_reach_outputs(
    canary: str, tmp_path, caplog
) -> None:
    vec = _corpus.load("valid_release_openssl")
    ctx = _corpus.build_context(vec)
    root = _corpus.materialize_artifacts(vec, tmp_path / "a")
    (root / "outputs/customers.csv").write_text(f"id,name\n1,{canary}\n")  # digest mismatch path
    doc = json.loads(vec.statement)
    doc["checks"][0]["reason_code"] = canary  # hostile producer smuggles text into a code field
    doc["checks"][0]["metrics"] = {"leak": canary}
    with caplog.at_level(logging.DEBUG):
        r = verify_signed_evidence(
            statement=rfc8785.dumps(doc), signature=vec.signature, artifact_root=root, context=ctx
        )
        r2 = verify_signed_evidence(
            statement=vec.statement, signature=vec.signature, artifact_root=root, context=ctx
        )
    for res in (r, r2):
        assert canary not in _surfaces(res)
        assert canary not in _all_check_text(res)
    assert canary not in caplog.text
    assert not r.exit_class.is_success and not r2.exit_class.is_success


@pytest.mark.parametrize("canary", CANARIES)
def test_exceptions_from_the_schema_layer_do_not_echo_values(canary: str) -> None:
    doc = json.loads(_corpus.load("valid_release_openssl").statement)
    for mutate in (
        lambda d: d["warnings"].append(canary),
        lambda d: d["counts"].update({canary: 1}),
        lambda d: d["artifacts"][0].update({"path": canary + "/../x"}),
        lambda d: d["issuer"].update({"tenant": canary + " tenant"}),
        lambda d: d.update({canary: 1}),
    ):
        d = json.loads(json.dumps(doc))
        mutate(d)
        with pytest.raises(SignedEvidenceError) as ei:
            st.parse_statement(rfc8785.dumps(d))
        assert canary not in str(ei.value) and canary not in repr(ei.value)
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", ei.value.code)


def test_reasons_and_notes_are_code_vocabulary_only() -> None:
    for vid in ("k_required_inconclusive_published", "a1_flip_csv", "t_stale_policy", "p_sig_der"):
        vec = _corpus.load(vid)
        try:
            ctx = _corpus.build_context(vec)
        except SignedEvidenceError:
            continue
        r = verify_signed_evidence(
            statement=vec.statement, signature=vec.signature, artifact_root=None, context=ctx
        )
        for code in r.reasons:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", code), code
        for note in r.notes:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*(:[A-Za-z0-9_./-]+)*", note), note


def test_metrics_values_are_restricted_to_an_allowlist_of_scalars() -> None:
    doc = json.loads(_corpus.load("valid_release_openssl").statement)
    for bad in ("alice@example.com", "SELECT * FROM t", "\\d+", 3.5, [1], {"k": 1}):
        d = json.loads(json.dumps(doc))
        d["checks"][0]["metrics"] = {"m": bad}
        with pytest.raises((SignedEvidenceError, ValueError, TypeError)):
            st.parse_statement(rfc8785.dumps(d))
