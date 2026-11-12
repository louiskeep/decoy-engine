"""The verdict blocks on anything it cannot read as a well-formed, known fact."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.config import RelationshipConfig
from decoy_engine.verification.postmask import (
    PostScanBlockedError,
    check_keys,
    from_table,
    keys_report,
    post_scan_verdict,
)
from decoy_engine.verification.postmask.runner import run_post_mask

CANARY = "CANARY-7d3f"
BASE = {
    "schema_version": "post-scan-integrity/v2",
    "residual_pii": [],
    "fk_preservation": [],
    "policy_validation": [],
    "pass_failed_with": None,
}
KeyShape = tuple[str, str, str, tuple[str, ...], bool, tuple[str, ...]]


def row(status: str = "checked", codes: Any = (), baseline: str = "measured") -> dict[str, Any]:
    return {
        "table": "c",
        "columns": ["k"],
        "kind": "foreign",
        "parent": ["p"],
        "status": status,
        "baseline": baseline,
        "bounded": True,
        "codes": codes,
        "source": None,
        "output": {},
    }


def code(name: str, namespace: Any = "post_scan") -> dict[str, Any]:
    return {"namespace": namespace, "code": name}


def names(rep: dict[str, Any]) -> list[str]:
    return [c.code for c in post_scan_verdict(rep).codes]


def with_keys(*keys: Any) -> dict[str, Any]:
    return {**BASE, "keys": list(keys)}


def finding(severity: str, **over: Any) -> dict[str, Any]:
    return {
        "table": "t",
        "column": "c",
        "detector_id": "email",
        "severity": severity,
        "message": CANARY,
        **over,
    }


# ----- finding 1: key rows --------------------------------------------------------------------


@pytest.mark.parametrize("codes", ["", {}, 0, None, False, "check_error", 7])
def test_a_key_whose_codes_are_not_a_list_blocks(codes: Any) -> None:
    assert names(with_keys(row("checked", codes))) == ["check_error"]


def test_a_key_with_no_codes_key_blocks() -> None:
    bare = row()
    del bare["codes"]
    assert names(with_keys(bare)) == ["check_error"]


@pytest.mark.parametrize("status", ["error", "key_column_missing", "key_type_mismatch"])
def test_a_trouble_status_with_an_empty_codes_list_blocks(status: str) -> None:
    assert names(with_keys(row(status, []))) == ["check_error"]
    assert names(with_keys(row(status, ()))) == ["check_error"]


@pytest.mark.parametrize("status", ["weird", "CHECKED", None, 3, ["checked"]])
def test_a_status_outside_the_engine_literals_blocks(status: Any) -> None:
    assert "check_error" in names(with_keys(row(status, [])))
    assert "check_error" in names(with_keys(row(status, [code("key_type_mismatch")])))


@pytest.mark.parametrize("baseline", [None, 3, ["measured"], "unknown"])
def test_a_missing_or_invalid_baseline_blocks(baseline: Any) -> None:
    key = row(baseline=baseline)
    assert names(with_keys(key)) == ["check_error"]
    key.pop("baseline")
    assert names(with_keys(key)) == ["check_error"]


@pytest.mark.parametrize(
    ("status", "baseline", "codes"),
    [
        ("error", "not_run", [code("key_column_missing")]),
        ("error", "not_run", [code("key_type_mismatch")]),
        ("key_column_missing", "not_run", [code("check_error")]),
        ("key_column_missing", "not_run", [code("key_type_mismatch")]),
        ("key_type_mismatch", "not_run", [code("check_error")]),
        ("key_type_mismatch", "not_run", [code("key_column_missing")]),
        ("key_type_mismatch", "not_run", [code("key_type_mismatch"), code("check_error")]),
    ],
)
def test_a_non_checked_status_with_the_wrong_code_set_blocks_as_check_error(
    status: str, baseline: str, codes: list[dict[str, Any]]
) -> None:
    assert names(with_keys(row(status, codes, baseline))) == ["check_error"]


@pytest.mark.parametrize(
    ("kind", "baseline", "codes"),
    [
        ("primary", "measured", [code("foreign_key_orphans_introduced")]),
        ("foreign", "measured", [code("primary_key_nulls_introduced")]),
        ("foreign", "error", [code("foreign_key_orphans_introduced")]),
        ("foreign", "key_type_mismatch", []),
        ("primary", "key_type_mismatch", [code("key_type_mismatch")]),
        (
            "primary",
            "measured",
            [code("primary_key_duplicates_introduced"), code("primary_key_nulls_introduced")],
        ),
    ],
)
def test_a_checked_status_with_an_unreachable_code_set_blocks_as_check_error(
    kind: str, baseline: str, codes: list[dict[str, Any]]
) -> None:
    key = row("checked", codes, baseline)
    key["kind"] = kind
    assert names(with_keys(key)) == ["check_error"]


@pytest.mark.parametrize(
    ("status", "baseline", "codes"),
    [
        *(
            ("error", baseline, [code("check_error")])
            for baseline in (
                "measured",
                "no_source",
                "key_column_missing",
                "key_type_mismatch",
                "error",
            )
        ),
        *(
            ("key_column_missing", baseline, [code("key_column_missing")])
            for baseline in (
                "measured",
                "no_source",
                "key_column_missing",
                "key_type_mismatch",
                "error",
            )
        ),
        ("key_type_mismatch", "measured", [code("key_type_mismatch")]),
        ("key_type_mismatch", "error", [code("key_type_mismatch")]),
        ("checked", "not_run", []),
    ],
)
def test_a_known_but_unreachable_status_and_baseline_pair_blocks_as_check_error(
    status: str, baseline: str, codes: list[dict[str, Any]]
) -> None:
    assert names(with_keys(row(status, codes, baseline))) == ["check_error"]


def reachable_key_shapes() -> list[KeyShape]:
    shapes: list[KeyShape] = [
        ("error", "not_run", "foreign", ("check_error",), True, ()),
        ("key_column_missing", "not_run", "foreign", ("key_column_missing",), True, ()),
    ]
    for baseline in ("key_type_mismatch", "not_run", "no_source", "key_column_missing"):
        shapes.append(
            (
                "key_type_mismatch",
                baseline,
                "foreign",
                ("key_type_mismatch",),
                baseline != "key_type_mismatch",
                ("key_type_mismatch",) if baseline == "key_type_mismatch" else (),
            )
        )
    primary_tails: tuple[tuple[str, ...], ...] = (
        (),
        ("primary_key_nulls_introduced",),
        ("primary_key_duplicates_introduced",),
        ("primary_key_nulls_introduced", "primary_key_duplicates_introduced"),
    )
    for baseline in ("measured", "no_source", "key_column_missing", "error", "key_type_mismatch"):
        prefix = (
            ("check_error",)
            if baseline == "error"
            else (("key_type_mismatch",) if baseline == "key_type_mismatch" else ())
        )
        warnings = prefix
        kinds_and_tails: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
            ("primary", primary_tails),
            ("foreign", ((), ("foreign_key_orphans_introduced",))),
        )
        for kind, tails in kinds_and_tails:
            if baseline == "key_type_mismatch" and kind == "primary":
                continue
            for tail in tails:
                shapes.append(("checked", baseline, kind, prefix + tail, bool(tail), warnings))
    return shapes


@pytest.mark.parametrize(
    ("status", "baseline", "kind", "codes", "blocking", "warnings"), reachable_key_shapes()
)
def test_every_key_shape_the_engine_can_write_keeps_its_verdict(
    status: str,
    baseline: str,
    kind: str,
    codes: tuple[str, ...],
    blocking: bool,
    warnings: tuple[str, ...],
) -> None:
    key = row(status, [code(name) for name in codes], baseline)
    key["kind"] = kind
    verdict = post_scan_verdict(with_keys(key))
    assert verdict.blocking is blocking
    assert tuple(item.code for item in verdict.codes) == tuple(
        name for name in codes if name not in warnings
    )
    assert tuple(item.code for item in verdict.warnings) == warnings


def test_a_checked_key_with_no_codes_is_the_one_clean_key() -> None:
    assert post_scan_verdict(with_keys(row("checked", []))).blocking is False


@pytest.mark.parametrize(
    "section", ["keys", "residual_pii", "fk_preservation", "policy_validation"]
)
def test_an_explicit_none_section_blocks_but_an_absent_one_does_not(section: str) -> None:
    # The engine writes a list for every section or leaves the key out (runner._report_to_dict).
    assert names({**BASE, section: None}) == ["check_error"]
    absent = {k: v for k, v in BASE.items() if k != section}
    assert post_scan_verdict(absent).blocking is False


def test_a_clean_report_built_by_the_engine_does_not_block(tmp_path: Path) -> None:
    rels = [
        RelationshipConfig.model_validate(
            {
                "parent": {"table": "p", "columns": ["id"]},
                "children": [{"table": "c", "columns": ["pid"]}],
                "orphan_policy": "warn",
            }
        )
    ]
    data = {
        "p": from_table("p", pa.table({"id": [1, 2]})),
        "c": from_table("c", pa.table({"pid": [1, 2]})),
    }
    found = check_keys(rels, data, data, scratch=tmp_path / "s", memory_limit="64MB")
    clean = {**run_post_mask({}, {}, config={}), "keys": keys_report(found)}
    assert clean["keys"] and clean["keys"][0]["status"] == "checked"
    verdict = post_scan_verdict(clean)
    assert (verdict.blocking, verdict.codes, verdict.warnings) == (False, (), ())


# ----- finding 2: identities and location text -------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        code(CANARY),
        code("check_error", CANARY),
        code(CANARY, CANARY),
        code("check_error", "plan"),
        code("key_type_mismatch", "scan"),
        code(CANARY, 123),
        code(123, "post_scan"),
        code("check_error", None),
        {"code": "check_error"},
    ],
)
def test_an_unregistered_or_malformed_identity_collapses_to_check_error(
    payload: dict[str, Any],
) -> None:
    rep = with_keys(row("checked", [payload]))
    verdict = post_scan_verdict(rep)
    assert [(c.namespace, c.code) for c in verdict.codes] == [("post_scan", "check_error")]
    assert verdict.warnings == ()
    assert CANARY not in repr(verdict.as_dict())


@pytest.mark.parametrize(
    "over",
    [
        {"table": 4242},
        {"table": ["x", CANARY]},
        {"table": ""},
        {"column": None},
        {"column": {CANARY: 1}},
        {"detector_id": None},
        {"detector_id": ""},
        {"detector_id": ["a", CANARY]},
        {"table": CANARY * 20},
        {"column": CANARY * 20},
        {"detector_id": CANARY * 20},
    ],
)
def test_a_spot_with_a_malformed_or_overlong_field_is_left_out(over: dict[str, Any]) -> None:
    rep = {**BASE, "residual_pii": [finding("fail", **over)]}
    error = PostScanBlockedError(rep)
    assert [c.code for c in error.verdict.codes] == ["finding_blocked"]
    summary = reasons.get("post_scan", "finding_blocked").summary
    assert error.message == str(error) == summary
    text = repr(error.verdict.as_dict()) + str(error) + error.message + error.reason
    assert CANARY not in text and "4242" not in text


def test_a_policy_spot_needs_a_valid_table_and_column_but_no_detector() -> None:
    good = {"table": "t", "column": "c", "severity": "fail", "message": CANARY}
    ok = PostScanBlockedError({**BASE, "policy_validation": [good]})
    assert ok.message.endswith("At t.c (policy_validation).")
    bad = PostScanBlockedError({**BASE, "policy_validation": [{**good, "table": 5}]})
    assert " At " not in bad.message


def test_a_spot_of_exactly_128_characters_is_kept_and_a_longer_one_is_not() -> None:
    keep = PostScanBlockedError({**BASE, "residual_pii": [finding("fail", table="t" * 128)]})
    assert "t" * 128 in keep.message
    drop = PostScanBlockedError({**BASE, "residual_pii": [finding("fail", table="t" * 129)]})
    assert " At " not in drop.message


# ----- finding 3: an unrelated key block keeps the FK reason ----------------------------------


def fk(severity: str) -> dict[str, Any]:
    return {"parent_table": "p", "child_table": "c", "severity": severity, "message": CANARY}


def test_an_unrelated_key_block_does_not_swallow_an_fk_fail() -> None:
    nulls = row("checked", [code("primary_key_nulls_introduced")])
    rep = {**with_keys(nulls), "fk_preservation": [fk("fail")]}
    assert names(rep) == ["primary_key_nulls_introduced", "foreign_key_orphans_introduced"]


def test_a_key_that_already_carries_the_orphans_code_gives_it_once_beside_an_fk_fail() -> None:
    orphans = row("checked", [code("foreign_key_orphans_introduced")])
    rep = {**with_keys(orphans), "fk_preservation": [fk("fail")]}
    assert names(rep) == ["foreign_key_orphans_introduced"]


def test_an_fk_error_adds_check_error_only_when_no_key_blocks() -> None:
    nulls = row("checked", [code("primary_key_nulls_introduced")])
    assert names({**with_keys(nulls), "fk_preservation": [fk("error")]}) == [
        "primary_key_nulls_introduced"
    ]
    assert names({**with_keys(row()), "fk_preservation": [fk("error")]}) == ["check_error"]
