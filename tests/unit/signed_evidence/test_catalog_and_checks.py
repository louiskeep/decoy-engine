"""RequiredCatalog loading and required-check semantics, one rule per test."""

from __future__ import annotations

import copy
import json

import pytest

from decoy_engine.signed_evidence import RequiredCatalog, SignedEvidenceError
from decoy_engine.signed_evidence import _checks as chk
from decoy_engine.signed_evidence import _statement as st
from decoy_engine.signed_evidence._context import ExpectedArtifact
from tests.unit.signed_evidence import _corpus
from tests.unit.signed_evidence import _factory as f

BASE = json.loads((_corpus.CORPUS / "shared" / "catalog-strict.json").read_text())


def _cat(doc: dict) -> RequiredCatalog:
    return RequiredCatalog.from_bytes(json.dumps(doc).encode())


def _code(doc: dict) -> str:
    with pytest.raises(SignedEvidenceError) as ei:
        _cat(doc)
    return ei.value.code


def test_loads_the_corpus_catalog_and_digest_is_over_canonical_bytes() -> None:
    a = _cat(BASE)
    b = RequiredCatalog.from_bytes(json.dumps(BASE, indent=4, sort_keys=True).encode())
    assert a.digest == b.digest, "formatting must not change the policy digest"
    assert a.has_required_snapshot_check


def test_vacuous_catalog_is_refused() -> None:
    d = copy.deepcopy(BASE)
    d["requirements"] = []
    assert _code(d) == "CATALOG_NO_REQUIRED_CHECKS"
    for r in d["requirements"]:
        r["optional"] = True
    d["requirements"] = copy.deepcopy(BASE["requirements"])
    for r in d["requirements"]:
        r["optional"] = True
    assert _code(d) == "CATALOG_NO_REQUIRED_CHECKS", "all-optional is as vacuous as empty"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d.update(extra=1), "CATALOG_SCHEMA_INVALID"),
        (lambda d: d.pop("id"), "CATALOG_SCHEMA_INVALID"),
        (lambda d: d.update(schema="decoy.required-catalog/v2"), "CATALOG_SCHEMA_UNSUPPORTED"),
        (lambda d: d.update(id="has space"), "CATALOG_SCHEMA_INVALID"),
        (lambda d: d.update(empty_output_allowed="yes"), "CATALOG_SCHEMA_INVALID"),
        (lambda d: d["requirements"][0].update(scope="table"), "CATALOG_SCHEMA_INVALID"),
        (lambda d: d["requirements"][0].update(media_types=[]), "CATALOG_SCHEMA_INVALID"),
        (
            lambda d: d["requirements"][0].update(media_types=["Not A Type"]),
            "CATALOG_SCHEMA_INVALID",
        ),
        (lambda d: d["requirements"][0].update(min_rows_scanned=-1), "CATALOG_SCHEMA_INVALID"),
        (lambda d: d["requirements"][0].update(min_rows_scanned=True), "CATALOG_SCHEMA_INVALID"),
        (lambda d: d["requirements"][0].update(optional=1), "CATALOG_SCHEMA_INVALID"),
        (
            lambda d: d["requirements"][0].update(not_applicable_reason_codes=["lower"]),
            "CATALOG_SCHEMA_INVALID",
        ),
        (lambda d: d["requirements"][0].update(check_id="Bad-Id"), "CATALOG_SCHEMA_INVALID"),
        (
            lambda d: d["requirements"].append(copy.deepcopy(d["requirements"][0])),
            "CATALOG_DUPLICATE_REQUIREMENT",
        ),
        (
            lambda d: d["requirements"][2].update(media_types=["text/csv"]),
            "CATALOG_SCHEMA_INVALID",
        ),  # snapshot scope
    ],
)
def test_catalog_schema_is_closed_and_strict(mutate, code) -> None:
    d = copy.deepcopy(BASE)
    mutate(d)
    assert _code(d) == code


def test_catalog_json_errors_are_prefixed_codes() -> None:
    with pytest.raises(SignedEvidenceError) as ei:
        RequiredCatalog.from_bytes(b'{"a":1,"a":2}')
    assert ei.value.code == "CATALOG_DUPLICATE_MEMBER"


# ----------------------------------------------------------------- check semantics


def _evaluate(mutate, catalog_mutate=None, expected=None):
    doc = f.base_draft()
    mutate(doc)
    cdoc = copy.deepcopy(BASE)
    if catalog_mutate:
        catalog_mutate(cdoc)
    catalog = _cat(cdoc)
    doc["policy"] = {"id": cdoc["id"], "version": cdoc["version"], "digest": catalog.digest}
    stmt = st.parse_statement(st.build_statement_bytes(doc))
    return chk.evaluate_checks(
        statement=stmt,
        catalog=catalog,
        expected_artifacts=expected if expected is not None else f.expected_artifacts(),
        expected_snapshot_id="snap-001",
    )


def _set(doc, cid, aid, **patch):
    for c in doc["checks"]:
        if c["check_id"] == cid and c["subject"]["id"] == aid:
            c.update(patch)


def test_baseline_passes_and_every_required_subject_is_accounted_for() -> None:
    ev = _evaluate(lambda d: None)
    assert ev.status.name == "PASS" and ev.reasons == ()
    required = [r for r in ev.results if r.required]
    assert len(required) == 6  # residual x2, snapshot, structural x3 (leakage is optional)


def test_min_rows_scanned_is_enforced_beyond_the_empty_subject_rule() -> None:
    def cat(d):
        for r in d["requirements"]:
            if r["check_id"] == "structural_integrity":
                r["min_rows_scanned"] = 5
        return d

    ev = _evaluate(lambda d: None, cat)  # orders has 12 rows, customers 3, quarantine 1
    assert ev.status.name == "INCONCLUSIVE" and "COVERAGE_INSUFFICIENT" in ev.reasons
    bad = {r.subject_id for r in ev.results if r.outcome == "INCONCLUSIVE"}
    assert bad == {"out_customers", "quarantine"}


def test_allow_empty_subject_permits_a_zero_row_pass_only_when_declared() -> None:
    def zero(d):
        _set(
            d,
            "structural_integrity",
            "quarantine",
            coverage={"rows_scanned": 0, "rows_total": 0, "columns": 2},
        )

    assert _evaluate(zero).status.name == "INCONCLUSIVE"

    def allow(d):
        for r in d["requirements"]:
            if r["check_id"] == "structural_integrity":
                r["allow_empty_subject"] = True
                r["min_rows_scanned"] = 0

    assert _evaluate(zero, allow).status.name == "PASS"


def test_scanned_all_rows_but_flagged_sampled_is_not_a_full_scan() -> None:
    def sampled(d):
        _set(d, "residual_detection", "out_customers", limits={"sampled": True, "sample_rows": 3})

    ev = _evaluate(sampled)
    assert ev.status.name == "INCONCLUSIVE" and "COVERAGE_INSUFFICIENT" in ev.reasons


def test_pass_with_error_severity_is_a_failure() -> None:
    ev = _evaluate(lambda d: _set(d, "structural_integrity", "out_orders", severity="error"))
    assert ev.status.name == "FAIL"


def test_warning_is_permitted_only_where_the_catalog_says_so() -> None:
    def warn(d):
        _set(d, "structural_integrity", "out_orders", severity="warning")

    def allow(d):
        for r in d["requirements"]:
            if r["check_id"] == "structural_integrity":
                r["allow_warning"] = True

    assert _evaluate(warn).status.name == "FAIL"
    assert _evaluate(warn, allow).status.name == "PASS"


def test_fail_outranks_inconclusive() -> None:
    def both(d):
        _set(
            d,
            "structural_integrity",
            "out_orders",
            status="FAIL",
            severity="error",
            reason_code="BAD",
        )
        _set(d, "structural_integrity", "quarantine", status="INCONCLUSIVE", reason_code="UNKNOWN")

    assert _evaluate(both).status.name == "FAIL"


def test_media_type_filters_decide_which_artifacts_are_subjects() -> None:
    """quarantine (ndjson) is not a residual-detection subject; customers/orders are."""
    ev = _evaluate(lambda d: None)
    residual = {r.subject_id for r in ev.results if r.check_id == "residual_detection"}
    assert residual == {"out_customers", "out_orders"}


def test_expected_subjects_come_from_the_caller_not_the_statement() -> None:
    """Drop an artifact from the caller's expectations: its checks become unknown, not satisfied."""
    fewer = tuple(a for a in f.expected_artifacts() if a.id != "quarantine")
    ev = _evaluate(lambda d: None, expected=fewer)
    assert ev.status.name == "INCONCLUSIVE" and "CHECK_UNKNOWN_SUBJECT" in ev.reasons


def test_snapshot_subject_uses_the_expected_snapshot() -> None:
    doc = f.base_draft()
    stmt = st.parse_statement(st.build_statement_bytes(doc))
    cat = _cat(BASE)
    other = chk.evaluate_checks(
        statement=stmt,
        catalog=cat,
        expected_artifacts=f.expected_artifacts(),
        expected_snapshot_id="snap-999",
    )
    assert other.status.name == "INCONCLUSIVE" and "CHECK_SUBJECT_MISSING" in other.reasons


def test_policy_version_mismatch_forces_inconclusive_even_when_every_outcome_passes() -> None:
    doc = f.base_draft()
    doc["policy"]["version"] = "999"
    stmt = st.parse_statement(st.build_statement_bytes(doc))
    ev = chk.evaluate_checks(
        statement=stmt,
        catalog=_cat(BASE),
        expected_artifacts=f.expected_artifacts(),
        expected_snapshot_id="snap-001",
    )
    assert ev.status.name == "INCONCLUSIVE" and "POLICY_VERSION_MISMATCH" in ev.reasons


def test_optional_checks_never_block_but_are_visible() -> None:
    ev = _evaluate(
        lambda d: _set(
            d,
            "leakage_sample",
            "out_customers",
            status="FAIL",
            severity="error",
            reason_code="LEAK",
        )
    )
    assert ev.status.name == "PASS"
    assert any(
        n.startswith("OPTIONAL_CHECK_FAIL:leakage_sample:artifact:out_customers") for n in ev.notes
    )


def test_expected_artifact_type_is_a_frozen_value() -> None:
    a = ExpectedArtifact(id="a", sha256="sha256:" + "0" * 64, media_type="text/csv")
    with pytest.raises(AttributeError):
        a.id = "b"  # type: ignore[misc]
    with pytest.raises(SignedEvidenceError):
        ExpectedArtifact(id="a", sha256="nope", media_type="text/csv")
