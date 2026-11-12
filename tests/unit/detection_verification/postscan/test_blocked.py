"""The one blocking decision: what a post-scan report blocks on, in which order, with what message."""

from __future__ import annotations

import pickle
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.errors import DecoyError
from decoy_engine.verification.postmask import (
    PostScanBlockedError,
    post_scan_verdict,
    severity_counts,
)

CANARY = "CANARY-9c2e"
ORDER = [
    "check_error",
    "key_column_missing",
    "key_type_mismatch",
    "primary_key_nulls_introduced",
    "primary_key_duplicates_introduced",
    "foreign_key_orphans_introduced",
    "finding_blocked",
    "ner_blocked",
]


def mapping(name: str) -> dict[str, str]:
    return {"namespace": "post_scan", "code": name}


def key(
    status: str = "checked", baseline: str = "measured", *codes: str, kind: str = "foreign"
) -> dict[str, Any]:
    return {
        "table": "c",
        "columns": ["k"],
        "kind": kind,
        "parent": ["p"],
        "status": status,
        "baseline": baseline,
        "bounded": True,
        "codes": [mapping(c) for c in codes],
        "source": None,
        "output": {},
    }


def residual(severity: str, table: str = "t", column: str = "c", detector: str = "email") -> Any:
    return {
        "table": table,
        "column": column,
        "detector_id": detector,
        "severity": severity,
        "message": f"leaked {CANARY}",
    }


def policy(severity: str, table: str = "t", column: str = "c") -> dict[str, Any]:
    return {
        "table": table,
        "column": column,
        "strategy": "hash",
        "severity": severity,
        "message": f"same {CANARY}",
    }


def fk(severity: str) -> dict[str, Any]:
    return {"parent_table": "p", "child_table": "c", "severity": severity, "message": CANARY}


def report(
    *,
    keys: list[dict[str, Any]] | None = None,
    res: tuple[Any, ...] = (),
    pol: tuple[Any, ...] = (),
    fks: tuple[Any, ...] = (),
    failed: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": "post-scan-integrity/v2",
        "residual_pii": list(res),
        "fk_preservation": list(fks),
        "policy_validation": list(pol),
        "pass_failed_with": failed,
    }
    if keys is not None:
        out["keys"] = keys
    return out


def blocking(rep: dict[str, Any], *, ner: bool = False) -> list[str]:
    return [c.code for c in post_scan_verdict(rep, ner_blocked=ner).codes]


# ----- AT-13 --------------------------------------------------------------------------------


def test_the_reason_is_the_first_code_in_the_documented_order_and_all_codes_follow_it() -> None:
    rep = report(
        keys=[
            key("error", "not_run", "check_error"),
            key("key_column_missing", "not_run", "key_column_missing"),
            key("key_type_mismatch", "not_run", "key_type_mismatch"),
            key("checked", "measured", "primary_key_nulls_introduced", kind="primary"),
            key("checked", "measured", "primary_key_duplicates_introduced", kind="primary"),
            key("checked", "measured", "foreign_key_orphans_introduced"),
        ],
        res=(residual("fail"),),
    )
    verdict = post_scan_verdict(rep, ner_blocked=True)
    assert verdict.blocking is True
    assert [c.code for c in verdict.codes] == ORDER
    assert verdict.codes[0].code == "check_error"
    assert verdict.warnings == ()


@pytest.mark.parametrize("drop", range(len(ORDER)))
def test_whichever_codes_are_present_the_first_one_in_order_leads(drop: int) -> None:
    cells = {
        "check_error": key("error", "not_run", "check_error"),
        "key_column_missing": key("key_column_missing", "not_run", "key_column_missing"),
        "key_type_mismatch": key("key_type_mismatch", "not_run", "key_type_mismatch"),
        "primary_key_nulls_introduced": key(
            "checked", "measured", "primary_key_nulls_introduced", kind="primary"
        ),
        "primary_key_duplicates_introduced": key(
            "checked", "measured", "primary_key_duplicates_introduced", kind="primary"
        ),
        "foreign_key_orphans_introduced": key(
            "checked", "measured", "foreign_key_orphans_introduced"
        ),
    }
    names = ORDER[drop:]
    rep = report(
        keys=[cells[n] for n in reversed(names) if n in cells],
        res=(residual("fail"),) if "finding_blocked" in names else (),
    )
    assert blocking(rep, ner="ner_blocked" in names) == names


def test_a_ner_block_and_a_key_block_make_one_verdict_with_both_codes() -> None:
    rep = report(keys=[key("checked", "measured", "foreign_key_orphans_introduced")])
    assert blocking(rep, ner=True) == ["foreign_key_orphans_introduced", "ner_blocked"]


# ----- AT-32 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("finding", "where"),
    [
        (residual("fail", "users", "email", "email"), "users.email (email)"),
        (policy("fail", "users", "ssn"), "users.ssn (policy_validation)"),
    ],
)
def test_a_residual_or_policy_fail_blocks_with_one_value_free_location(
    finding: dict[str, Any], where: str
) -> None:
    rep = report(res=(finding,)) if "detector_id" in finding else report(pol=(finding,))
    verdict = post_scan_verdict(rep)
    assert verdict.blocking and [c.code for c in verdict.codes] == ["finding_blocked"]
    error = PostScanBlockedError(rep)
    summary = reasons.get("post_scan", "finding_blocked").summary
    assert str(error) == f"{summary} At {where}."
    assert CANARY not in str(error) + error.message + repr(verdict)


@pytest.mark.parametrize("severity", ["info", "warning"])
def test_info_and_warning_findings_do_not_block(severity: str) -> None:
    rep = report(res=(residual(severity),), pol=(policy(severity),), fks=(fk(severity),))
    verdict = post_scan_verdict(rep)
    assert (verdict.blocking, verdict.codes, verdict.warnings) == (False, (), ())


@pytest.mark.parametrize(
    "rep",
    [
        report(res=(residual("error"),)),
        report(pol=(policy("error"),)),
        report(failed="TypeError"),
    ],
)
def test_an_error_finding_or_a_failed_pass_gives_check_error(rep: dict[str, Any]) -> None:
    assert blocking(rep) == ["check_error"]


def test_an_fk_finding_derived_from_a_key_adds_no_duplicate_code() -> None:
    orphans = key("checked", "measured", "foreign_key_orphans_introduced")
    assert blocking(report(keys=[orphans], fks=(fk("fail"),))) == ["foreign_key_orphans_introduced"]
    missing = key("key_column_missing", "not_run", "key_column_missing")
    assert blocking(report(keys=[missing], fks=(fk("error"),))) == ["key_column_missing"]


def test_an_fk_finding_with_no_key_facts_still_blocks_with_its_own_code() -> None:
    assert blocking(report(fks=(fk("fail"),))) == ["foreign_key_orphans_introduced"]
    assert blocking(report(fks=(fk("error"),))) == ["check_error"]


def test_the_verdict_reads_codes_not_status() -> None:
    errored = report(keys=[key("error", "not_run", "check_error")])
    assert blocking(errored) == ["check_error"]
    clean = report(keys=[key("checked", "measured")])
    verdict = post_scan_verdict(clean)
    assert (verdict.blocking, verdict.codes, verdict.warnings) == (False, (), ())


@pytest.mark.parametrize(
    ("status", "baseline", "block"),
    [
        ("key_type_mismatch", "key_type_mismatch", False),
        ("key_type_mismatch", "not_run", True),
        ("key_type_mismatch", "no_source", True),
        ("key_type_mismatch", "key_column_missing", True),
    ],
)
def test_an_output_mismatch_blocks_or_warns_by_its_baseline(
    status: str, baseline: str, block: bool
) -> None:
    verdict = post_scan_verdict(report(keys=[key(status, baseline, "key_type_mismatch")]))
    assert verdict.blocking is block
    assert [c.code for c in verdict.codes] == (["key_type_mismatch"] if block else [])
    assert [c.code for c in verdict.warnings] == ([] if block else ["key_type_mismatch"])


def test_a_source_side_warning_rides_beside_an_introduced_code_and_never_leads() -> None:
    rep = report(
        keys=[
            key("checked", "error", "check_error", "foreign_key_orphans_introduced"),
            key("checked", "key_type_mismatch", "key_type_mismatch"),
        ]
    )
    verdict = post_scan_verdict(rep)
    assert [c.code for c in verdict.codes] == ["foreign_key_orphans_introduced"]
    assert [c.code for c in verdict.warnings] == ["check_error", "key_type_mismatch"]
    only = report(keys=[key("checked", "error", "check_error")])
    assert (
        post_scan_verdict(only).blocking,
        [c.code for c in post_scan_verdict(only).warnings],
    ) == (
        False,
        ["check_error"],
    )


def test_the_verdict_dict_carries_namespace_and_code_mappings() -> None:
    rep = report(
        keys=[
            key("checked", "measured", "foreign_key_orphans_introduced"),
            key("checked", "key_type_mismatch", "key_type_mismatch"),
        ]
    )
    assert post_scan_verdict(rep).as_dict() == {
        "blocking": True,
        "codes": [mapping("foreign_key_orphans_introduced")],
        "warnings": [mapping("key_type_mismatch")],
    }


def test_severity_counts_tally_the_four_severities() -> None:
    counts = severity_counts(["info", "info", "warning", "fail", "error", "error", "other"])
    assert (counts.passed, counts.warning, counts.fail, counts.error) == (2, 1, 1, 2)


# ----- an unreadable report fails closed ---------------------------------------------------


class _Row:
    """A caller-shape mistake: a result object where the report holds a dict."""


@pytest.mark.parametrize(
    "rep",
    [
        report(keys=[_Row()]),  # type: ignore[list-item]
        report(res=(_Row(),)),
        report(pol=("not a mapping",)),
        report(fks=(["row"],)),
        report(keys={"a": "b"}),  # type: ignore[arg-type]
        {**report(), "residual_pii": {"t": 1}},
        {**report(), "fk_preservation": "oops"},
        report(keys=[{**key(), "codes": "check_error"}]),
        report(keys=[{**key(), "codes": ["check_error"]}]),
        report(keys=[{**key(), "codes": {"namespace": "post_scan"}}]),
    ],
)
def test_a_section_or_row_the_verdict_cannot_read_blocks_on_check_error(
    rep: dict[str, Any],
) -> None:
    verdict = post_scan_verdict(rep)
    assert verdict.blocking is True
    assert "check_error" in [c.code for c in verdict.codes]


@pytest.mark.parametrize("version", ["post-scan-integrity/v3", "post-scan-integrity/v1", None, 2])
def test_a_report_of_another_schema_major_blocks_on_check_error(version: Any) -> None:
    rep = report()
    if version is None:
        del rep["schema_version"]
    else:
        rep["schema_version"] = version
    assert blocking(rep) == ["check_error"]
    assert post_scan_verdict(report()).blocking is False


@pytest.mark.parametrize("section", ["residual_pii", "policy_validation", "fk_preservation"])
@pytest.mark.parametrize("severity", ["critical", "FAIL", None, 3])
def test_an_unknown_severity_in_a_blocking_section_blocks_on_check_error(
    section: str, severity: Any
) -> None:
    rep = {**report(), section: [{"table": "t", "column": "c", "severity": severity}]}
    assert blocking(rep) == ["check_error"]


def test_a_source_side_check_error_on_an_errored_key_still_blocks() -> None:
    assert blocking(report(keys=[key("error", "error", "check_error")])) == ["check_error"]


def test_a_verdict_blocked_only_by_a_key_code_names_no_findings_location() -> None:
    keyed = report(keys=[key("checked", "measured", "foreign_key_orphans_introduced")])
    leaky = report(res=(residual("fail", "users", "email"),))
    error = PostScanBlockedError(leaky, post_scan_verdict(keyed))
    assert str(error) == reasons.get("post_scan", "foreign_key_orphans_introduced").summary
    assert "users" not in str(error) and " At " not in str(error)


# ----- the exception ------------------------------------------------------------------------


def test_the_exception_is_a_decoy_error_with_a_picklable_value_free_report() -> None:
    rep = report(keys=[key("checked", "measured", "foreign_key_orphans_introduced")])
    error = PostScanBlockedError(rep, post_scan_verdict(rep, ner_blocked=True))
    assert isinstance(error, DecoyError)
    assert error.report == rep
    assert error.reason == "post_scan.foreign_key_orphans_introduced"
    assert str(error) == reasons.get("post_scan", "foreign_key_orphans_introduced").summary
    again = pickle.loads(pickle.dumps(error))  # noqa: S301 - a round trip of our own value
    assert [c.code for c in again.verdict.codes] == [
        "foreign_key_orphans_introduced",
        "ner_blocked",
    ]
    assert error.__cause__ is None and error.__context__ is None
