"""EvidenceStatementV1: strict schema, one canonical representation per value (plan 1.4)."""

from __future__ import annotations

import copy
import json

import pytest
import rfc8785

from decoy_engine.signed_evidence import SignedEvidenceError
from decoy_engine.signed_evidence import _statement as st
from tests.unit.signed_evidence import _corpus


def _base() -> dict:
    return json.loads(_corpus.load("valid_release_openssl").statement)


def _bytes(d: dict) -> bytes:
    return rfc8785.dumps(d)


def _err(d: dict) -> SignedEvidenceError:
    with pytest.raises(SignedEvidenceError) as ei:
        st.parse_statement(_bytes(d))
    return ei.value


def test_valid_statement_parses_to_frozen_typed_value() -> None:
    s = st.parse_statement(_bytes(_base()))
    assert s.job_id == "job-1001" and s.publication.state == "PUBLISHED"
    assert [a.id for a in s.artifacts] == ["out_customers", "out_orders", "quarantine"]
    with pytest.raises(AttributeError):
        s.job_id = "x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        s.artifacts[0].sha256 = "x"  # type: ignore[misc]


def test_statement_id_is_sha256_of_the_exact_bytes() -> None:
    import hashlib

    raw = _bytes(_base())
    assert st.parse_statement(raw).statement_id == "sha256:" + hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("job_id",), "job id with spaces", "STATEMENT_SCHEMA_INVALID"),
        (("job_id",), "", "STATEMENT_SCHEMA_INVALID"),
        (("job_id",), 7, "STATEMENT_SCHEMA_INVALID"),
        (("created_at",), "2026-10-10 08:00:00", "STATEMENT_SCHEMA_INVALID"),
        (("created_at",), "2026-13-10T08:00:00Z", "STATEMENT_SCHEMA_INVALID"),
        (("created_at",), "2026-10-10T08:00:00+00:00", "STATEMENT_SCHEMA_INVALID"),
        (("counts", "rows_masked"), -1, "STATEMENT_SCHEMA_INVALID"),
        (("counts", "rows_masked"), True, "STATEMENT_SCHEMA_INVALID"),
        (("counts", "Bad-Name"), 1, "STATEMENT_SCHEMA_INVALID"),
        (("counts", "rows_masked"), "012", "STATEMENT_SCHEMA_INVALID"),
        (("counts", "rows_masked"), "9007199254740991", "STATEMENT_SCHEMA_INVALID"),
        (("dispositions", "masking"), "free text with spaces", "STATEMENT_SCHEMA_INVALID"),
        (("warnings",), ["B_CODE", "A_CODE"], "STATEMENT_ARRAY_ORDER"),
        (("warnings",), ["A_CODE", "A_CODE"], "STATEMENT_ARRAY_ORDER"),
        (("warnings",), ["not a code"], "STATEMENT_SCHEMA_INVALID"),
        (("publication", "state"), "MAYBE", "STATEMENT_SCHEMA_INVALID"),
        (("publication", "reason_code"), "SOMETHING", "STATEMENT_PUBLICATION_INCOHERENT"),
        (("policy", "digest"), "sha256:abc", "STATEMENT_SCHEMA_INVALID"),
        (("policy", "digest"), "SHA256:" + "a" * 64, "STATEMENT_SCHEMA_INVALID"),
        (("source", "snapshot_id"), None, "STATEMENT_SCHEMA_INVALID"),
        (("artifacts", 0, "sha256"), "sha256:" + "A" * 64, "STATEMENT_SCHEMA_INVALID"),
        (("artifacts", 0, "bytes"), -5, "STATEMENT_SCHEMA_INVALID"),
        (("artifacts", 0, "disposition"), "LOST", "STATEMENT_SCHEMA_INVALID"),
        (("artifacts", 0, "media_type"), "not a media type", "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "status"), "MAYBE", "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "severity"), "fatal", "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "reason_code"), "KeyError: 'secret'", "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "reason_code"), "/etc/passwd", "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "metrics"), {"email": "alice@example.com"}, "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "metrics"), {"ratio": "0.25.1"}, "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "metrics"), {"Bad Key": 1}, "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "metrics"), {"nested": {"a": 1}}, "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "metrics"), {"rows": [1, 2]}, "STATEMENT_SCHEMA_INVALID"),
        (
            ("checks", 0, "coverage"),
            {"rows_scanned": 4, "rows_total": 3, "columns": 1},
            "STATEMENT_COVERAGE_INVALID",
        ),
        (("checks", 0, "coverage", "columns"), -1, "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "limits", "sampled"), "yes", "STATEMENT_SCHEMA_INVALID"),
        (("checks", 0, "subject", "kind"), "table", "STATEMENT_SCHEMA_INVALID"),
        (("engine", "name"), "decoy engine", "STATEMENT_SCHEMA_INVALID"),
        (("fingerprints", 0, "value"), "not a digest!", "STATEMENT_SCHEMA_INVALID"),
    ],
)
def test_field_rules(path: tuple, value: object, code: str) -> None:
    d = _base()
    node = d
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = value
    assert _err(d).code == code


def test_decimal_string_counts_use_one_representation() -> None:
    d = _base()
    d["counts"]["rows_masked"] = "9007199254740992"  # first value outside the safe-integer domain
    assert st.parse_statement(_bytes(d)).counts["rows_masked"] == 9007199254740992
    d["counts"]["rows_masked"] = (
        "9007199254740991"  # fits as an integer: the string form is invalid
    )
    assert _err(d).code == "STATEMENT_SCHEMA_INVALID"


def test_missing_and_extra_members_are_rejected_at_every_level() -> None:
    for path in [
        (),
        ("issuer",),
        ("source",),
        ("source", "fingerprint"),
        ("publication",),
        ("artifacts", 0),
        ("checks", 0),
        ("checks", 0, "coverage"),
        ("checks", 0, "subject"),
        ("policy",),
        ("engine",),
    ]:
        d = _base()
        node = d
        for p in path:
            node = node[p]
        d2 = copy.deepcopy(d)
        node["__extra__"] = 1
        assert _err(d).code == "STATEMENT_UNKNOWN_FIELD", path
        # removing any required member is just as fatal
        d3 = _base()
        n3 = d3
        for p in path:
            n3 = n3[p]
        victim = next(iter(n3))
        del n3[victim]
        assert _err(d3).code in {"STATEMENT_SCHEMA_INVALID", "STATEMENT_MISSING_FIELD"}, (
            path,
            victim,
        )
        del d2


def test_publication_coherence_rules() -> None:
    d = _base()
    d["publication"] = {"state": "REJECTED", "reason_code": None}
    assert _err(d).code == "STATEMENT_PUBLICATION_INCOHERENT"
    d["publication"] = {"state": "REJECTED", "reason_code": "CHECK_FAILED"}
    assert (
        _err(d).code == "STATEMENT_PUBLICATION_INCOHERENT"
    )  # REJECTED may not carry INCLUDED data
    for a in d["artifacts"]:
        a["disposition"] = "WITHHELD"
    assert st.parse_statement(_bytes(d)).publication.state == "REJECTED"
    p = _base()
    p["artifacts"][0]["disposition"] = "WITHHELD"
    assert _err(p).code == "STATEMENT_PUBLICATION_INCOHERENT"  # PUBLISHED may not withhold


def test_unavailable_artifact_has_no_digest_and_included_requires_one() -> None:
    d = _base()
    a = d["artifacts"][0]
    a["disposition"] = "UNAVAILABLE"
    d["publication"] = {"state": "REJECTED", "reason_code": "ARTIFACT_UNAVAILABLE"}
    for other in d["artifacts"]:
        other["disposition"] = "WITHHELD"
    a["disposition"] = "UNAVAILABLE"
    assert _err(d).code == "STATEMENT_SCHEMA_INVALID"  # digest present on an UNAVAILABLE artifact
    a["bytes"] = None
    a["sha256"] = None
    assert st.parse_statement(_bytes(d)).artifacts[0].sha256 is None
    e = _base()
    e["artifacts"][0]["sha256"] = None
    assert _err(e).code == "STATEMENT_SCHEMA_INVALID"


def test_check_array_order_is_schema_defined() -> None:
    d = _base()
    d["checks"][0], d["checks"][1] = d["checks"][1], d["checks"][0]
    assert _err(d).code == "STATEMENT_ARRAY_ORDER"


def test_duplicate_check_outcomes_are_representable_so_the_verifier_can_flag_them() -> None:
    d = _base()
    d["checks"].insert(1, copy.deepcopy(d["checks"][1]))
    assert len(st.parse_statement(_bytes(d)).checks) == len(d["checks"])


def test_count_limits_are_enforced() -> None:
    d = _base()
    d["artifacts"] = [
        {**d["artifacts"][0], "id": f"a{i:06d}", "path": f"p/{i}.csv"}
        for i in range(st.MAX_ARTIFACTS + 1)
    ]
    assert _err(d).code == "STATEMENT_TOO_MANY_ARTIFACTS"


@pytest.mark.parametrize(
    "path",
    [
        "a/b.csv",
        "r\u00e9sum\u00e9/\U0001f600.csv",
        "x" * 200,
        "dir/sub/file with space.csv",
        "a.b/c..d",
    ],
)
def test_safe_relative_paths_are_accepted_verbatim(path: str) -> None:
    d = _base()
    d["artifacts"][0]["path"] = path
    assert st.parse_statement(_bytes(d)).artifacts[0].path == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/abs",
        "../x",
        "a/../b",
        "a/./b",
        "a//b",
        "a/",
        "a\\b",
        "C:/x",
        "a\x00b",
        "a\nb",
        "a\u202eb",
        "x" * 1025,
        " lead",
        "trail ",
        ".",
    ],
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    d = _base()
    d["artifacts"][0]["path"] = path
    assert _err(d).code == "STATEMENT_PATH_INVALID"


def test_nfc_and_nfd_paths_are_distinct_and_not_normalized() -> None:
    nfc, nfd = "caf\u00e9.csv", "cafe\u0301.csv"
    d = _base()
    d["artifacts"][0]["path"] = nfc
    d["artifacts"][1]["path"] = nfd
    s = st.parse_statement(_bytes(d))
    assert {s.artifacts[0].path, s.artifacts[1].path} == {nfc, nfd}


def test_noncanonical_input_is_reported_as_such_not_silently_normalized() -> None:
    raw = json.dumps(_base(), indent=2).encode()
    with pytest.raises(SignedEvidenceError) as ei:
        st.parse_statement(raw)
    assert ei.value.code == "STATEMENT_NOT_CANONICAL" and ei.value.status == "NON_CANONICAL"


def test_build_statement_bytes_validates_and_canonicalizes_and_does_not_alias_the_draft() -> None:
    draft = _base()
    out = st.build_statement_bytes(draft)
    assert out == rfc8785.dumps(draft)
    draft["job_id"] = "mutated-after-build"
    assert json.loads(out)["job_id"] == "job-1001"
    bad = _base()
    bad["extra"] = 1
    with pytest.raises(SignedEvidenceError):
        st.build_statement_bytes(bad)
