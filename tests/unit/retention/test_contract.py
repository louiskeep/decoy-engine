"""retention-policy/v1 and retention-report/v1 contract tests (F26 Slice 0)."""

from __future__ import annotations

import json
import re

import pytest

from decoy_engine import retention as r

ID_A = "a" * 32
ID_B = "b" * 32
LIMITS = r.Limits(max_items=100, max_bytes=10**9, max_backend_calls=500, max_seconds=600)
DIGEST = "0" * 64


def _item(aid=ID_A, eligible=True, reason=None, **kw):
    reason = reason or (r.ReasonCode.ELIGIBLE if eligible else r.ReasonCode.RETAIN_HOLD)
    return r.ReportItem(
        artifact_id=aid,
        artifact_class=r.ArtifactClass.RAW_UPLOAD,
        decision=r.Decision.ELIGIBLE if eligible else r.Decision.RETAINED,
        reason=reason,
        size_bytes=10,
        **kw,
    )


def _report(items, **kw):
    kind = kw.pop("kind", r.ReportKind.PREVIEW)
    counts = kw.pop("counts", r.counts_from_items(items))
    return r.RetentionReport(
        kind=kind,
        run_id=ID_B,
        policy_id=r.DEFAULT_POLICY.policy_id,
        scope=(r.ArtifactClass.RAW_UPLOAD,),
        complete=kw.pop("complete", True),
        limits=LIMITS,
        counts=counts,
        digest=DIGEST,
        items=tuple(items),
        **kw,
    )


def test_canonical_json_is_byte_stable_across_a_round_trip():
    rep = _report([_item(), _item(ID_B, eligible=False)])
    again = r.RetentionReport.from_dict(json.loads(rep.to_json()))
    assert again.to_json() == rep.to_json()
    assert " " not in rep.to_json()


def test_report_serializes_only_allow_listed_keys():
    d = _report([_item()]).to_dict()
    assert set(d) == r.ALLOWED_REPORT_KEYS
    assert set(d["items"][0]) == r.ALLOWED_ITEM_KEYS
    assert set(d["limits"]) == r.ALLOWED_LIMIT_KEYS
    assert set(d["counts"]) == r.ALLOWED_COUNT_KEYS


@pytest.mark.parametrize("bad", ["/etc/passwd", "report.csv", "A" * 32, "a" * 31, "s3://b/k", ""])
def test_artifact_id_must_be_opaque(bad):
    with pytest.raises(ValueError):
        _item(aid=bad)


def test_error_type_must_be_an_identifier_not_a_message():
    with pytest.raises(ValueError):
        _item(error_type="boom: /data/secret.csv", eligible=False)
    assert _item(error_type="ClientError", eligible=False).error_type == "ClientError"


def test_decision_and_reason_cannot_disagree():
    with pytest.raises(ValueError):
        r.ReportItem(ID_A, r.ArtifactClass.STAGING, r.Decision.ELIGIBLE, r.ReasonCode.RETAIN_HOLD)
    with pytest.raises(ValueError):
        r.ReportItem(ID_A, r.ArtifactClass.STAGING, r.Decision.RETAINED, r.ReasonCode.ELIGIBLE)


def test_unknown_future_values_parse_to_retained():
    item = r.ReportItem.from_dict(
        {
            "artifact_id": ID_A,
            "artifact_class": "hologram",
            "decision": "eligible",
            "reason": "some_future_reason",
        }
    )
    assert item.decision is r.Decision.RETAINED
    assert item.reason is r.ReasonCode.RETAIN_UNKNOWN
    assert item.artifact_class is r.ArtifactClass.UNKNOWN


def test_a_future_eligible_decision_with_a_retain_reason_is_retained_not_eligible():
    item = r.ReportItem.from_dict(
        {
            "artifact_id": ID_A,
            "artifact_class": "staging",
            "decision": "eligible",
            "reason": "retain_hold",
        }
    )
    assert item.decision is r.Decision.RETAINED
    assert item.reason is r.ReasonCode.RETAIN_HOLD


def test_report_rejects_unknown_fields_and_schemas():
    d = _report([_item()]).to_dict()
    with pytest.raises(ValueError):
        r.RetentionReport.from_dict({**d, "path": "/x"})
    with pytest.raises(ValueError):
        r.RetentionReport.from_dict({**d, "schema": "retention-report/v2"})
    bad_item = {**d["items"][0], "filename": "x.csv"}
    with pytest.raises(ValueError):
        r.RetentionReport.from_dict({**d, "items": [bad_item]})


def test_every_reason_has_a_fixed_summary_and_a_family():
    for code in r.ReasonCode:
        assert code in r.REASON_SUMMARY, code
        assert code.family in {"eligible", "retain", "result", "refused", "unknown"}
        assert re.fullmatch(r"[a-z_]+", code.value)


def test_reason_summaries_are_fixed_strings_with_no_format_fields():
    for text in r.REASON_SUMMARY.values():
        assert "{" not in text and "%" not in text


def test_exit_codes_are_stable_and_distinct():
    assert {e.name: int(e) for e in r.ExitCode} == {
        "SUCCESS": 0,
        "NOOP": 10,
        "RETAINED": 11,
        "PARTIAL": 12,
        "REFUSED": 13,
        "INDETERMINATE": 14,
    }
    # outcome codes sit clear of every consumer's own usage/runtime codes (CLI 1-5, argparse 2)
    assert all(int(e) == 0 or int(e) >= 10 for e in r.ExitCode)
    assert r.exit_code_for(r.RunOutcome.UNKNOWN) is r.ExitCode.INDETERMINATE != r.ExitCode.SUCCESS


@pytest.mark.parametrize(
    "kind,counts,complete,refusal,expected",
    [
        (r.ReportKind.PREVIEW, r.Counts(), True, None, r.RunOutcome.NOOP),
        (r.ReportKind.PREVIEW, r.Counts(total=2, retained=2), True, None, r.RunOutcome.RETAINED),
        (
            r.ReportKind.PREVIEW,
            r.Counts(total=2, eligible=1, retained=1),
            True,
            None,
            r.RunOutcome.SUCCESS,
        ),
        (
            r.ReportKind.PREVIEW,
            r.Counts(total=2, eligible=1, retained=1),
            False,
            None,
            r.RunOutcome.PARTIAL,
        ),
        (r.ReportKind.EXECUTE, r.Counts(approved=0), True, None, r.RunOutcome.NOOP),
        (r.ReportKind.EXECUTE, r.Counts(approved=3, deleted=3), True, None, r.RunOutcome.SUCCESS),
        (r.ReportKind.EXECUTE, r.Counts(approved=3, deleted=0), True, None, r.RunOutcome.RETAINED),
        (
            r.ReportKind.EXECUTE,
            r.Counts(approved=3, deleted=2, failed=1),
            True,
            None,
            r.RunOutcome.PARTIAL,
        ),
        (
            r.ReportKind.EXECUTE,
            r.Counts(approved=3, deleted=0, failed=3),
            True,
            None,
            r.RunOutcome.PARTIAL,
        ),
        (r.ReportKind.EXECUTE, r.Counts(approved=3, deleted=2), True, None, r.RunOutcome.PARTIAL),
        (
            r.ReportKind.EXECUTE,
            r.Counts(approved=3, deleted=3),
            True,
            r.ReasonCode.REFUSED_GATE_OFF,
            r.RunOutcome.REFUSED,
        ),
    ],
)
def test_outcome_derivation_is_one_shared_mapping(kind, counts, complete, refusal, expected):
    assert r.derive_outcome(kind, counts, complete=complete, refusal=refusal) is expected


def test_refusal_must_be_a_refused_code():
    with pytest.raises(ValueError):
        _report([], refusal=r.ReasonCode.RETAIN_HOLD)


def _digest(**over):
    args = dict(
        policy_id=r.DEFAULT_POLICY.policy_id,
        scope=[r.ArtifactClass.RAW_UPLOAD],
        limits=LIMITS,
        expires_at=1000,
        eligible=[(ID_A, "ident-a"), (ID_B, "ident-b")],
    )
    args.update(over)
    return r.inventory_digest(**args)


def test_digest_is_order_independent_and_hex():
    assert _digest() == _digest(eligible=[(ID_B, "ident-b"), (ID_A, "ident-a")])
    assert re.fullmatch(r"[0-9a-f]{64}", _digest())


@pytest.mark.parametrize(
    "over",
    [
        {"eligible": [(ID_A, "ident-a")]},  # narrowing
        {"eligible": [(ID_A, "ident-a"), (ID_B, "ident-b"), ("c" * 32, "x")]},  # widening
        {"eligible": [(ID_A, "ident-a"), (ID_B, "swapped")]},  # identity swap
        {"scope": [r.ArtifactClass.RAW_UPLOAD, r.ArtifactClass.STAGING]},
        {"expires_at": 1001},
        {
            "limits": r.Limits(
                max_items=101, max_bytes=10**9, max_backend_calls=500, max_seconds=600
            )
        },
        {"policy_id": "f" * 16},
    ],
)
def test_digest_changes_when_anything_it_binds_changes(over):
    assert _digest(**over) != _digest()


def test_identity_digests_never_reach_the_serialized_report():
    ident = "SECRET-IDENTITY-MARKER"
    d = _digest(eligible=[(ID_A, ident)])
    rep = _report([_item()])
    assert ident not in rep.to_json() and ident not in r.render_text(rep) and ident not in d


def test_text_output_is_counts_and_opaque_ids_only():
    out = r.render_text(_report([_item(), _item(ID_B, eligible=False)]))
    assert "retain_hold: 1" in out and "total=2" in out
    body = out.split("\n", 1)[1]  # the header names the schema, which contains a slash
    assert not re.search(r"[/\\]|\.csv|\.parquet", body)


def test_default_policy_is_conservative():
    p = r.DEFAULT_POLICY
    for key in (
        r.PolicyKey.RAW_UPLOAD_SAVED,
        r.PolicyKey.SNAPSHOT,
        r.PolicyKey.PUBLISHED_OUTPUT,
        r.PolicyKey.CHECKPOINT,
    ):
        assert p.for_key(key).mode is r.PolicyMode.KEEP_ALL
    for key in (r.PolicyKey.RAW_UPLOAD_TRANSIENT, r.PolicyKey.STAGING):
        cp = p.for_key(key)
        assert cp.mode is r.PolicyMode.EXPIRE_AFTER_QUIESCENCE and cp.window_hours == 24


def test_policy_round_trip_and_content_addressed_id():
    p = r.RetentionPolicy.from_dict(r.DEFAULT_POLICY.to_dict())
    assert p.policy_id == r.DEFAULT_POLICY.policy_id
    changed = dict(r.DEFAULT_POLICY.classes)
    changed[r.PolicyKey.STAGING] = r.ClassPolicy(r.PolicyMode.EXPIRE_AFTER_QUIESCENCE, 48)
    assert r.RetentionPolicy(changed).policy_id != p.policy_id


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema="retention-policy/v2"),
        lambda d: d["classes"].pop("staging"),
        lambda d: d["classes"].update(mystery={"mode": "keep_all", "window_hours": None}),
        lambda d: d["classes"]["staging"].update(window_hours=0),
        lambda d: d["classes"]["staging"].update(window_hours=r.MAX_WINDOW_HOURS + 1),
        lambda d: d["classes"]["staging"].update(window_hours=True),
        lambda d: d["classes"]["staging"].update(window_hours="24"),
        lambda d: d["classes"]["snapshot"].update(window_hours=5),
        lambda d: d["classes"]["staging"].update(mode="delete_now"),
        lambda d: d["classes"]["staging"].update(extra=1),
    ],
)
def test_policy_parse_is_strict(mutate):
    d = json.loads(json.dumps(r.DEFAULT_POLICY.to_dict()))
    mutate(d)
    with pytest.raises(ValueError):
        r.RetentionPolicy.from_dict(d)


def test_evidence_is_not_a_deletion_target():
    assert r.ArtifactClass.EVIDENCE not in r.TARGET_CLASSES
    assert set(r.TARGET_CLASSES) == {
        r.ArtifactClass.RAW_UPLOAD,
        r.ArtifactClass.SNAPSHOT,
        r.ArtifactClass.PUBLISHED_OUTPUT,
        r.ArtifactClass.STAGING,
        r.ArtifactClass.CHECKPOINT,
    }


KNOWN_DIGEST = "60a9bac3737bccff1cebe48ad3a6805963e24945044529950e93695fc435025d"


def test_digest_known_answer_pins_the_algorithm_domain_included():
    """Platform and CLI must compute the same digest for the same set, forever. A change to the
    domain string, the field set or the encoding changes this value and must be a deliberate,
    versioned edit. (Independently derived: sha256 of the sorted-key compact JSON of the body.)"""
    got = r.inventory_digest(
        policy_id="7ead38cea6e6aa84",
        scope=[r.ArtifactClass.RAW_UPLOAD, r.ArtifactClass.STAGING],
        limits=r.Limits(max_items=10, max_bytes=1000, max_backend_calls=5, max_seconds=60),
        expires_at=1_700_000_000,
        eligible=[("a" * 32, "ident-a"), ("b" * 32, "ident-b")],
    )
    assert got == KNOWN_DIGEST


def test_canonical_json_sorts_keys_and_has_no_whitespace():
    assert r.canonical_json({"b": 1, "a": [2, {"d": 1, "c": 2}]}) == '{"a":[2,{"c":2,"d":1}],"b":1}'
    with pytest.raises(ValueError):
        r.canonical_json({"x": float("nan")})


def test_cloud_object_codes_are_retained_statuses_never_executable():
    """Decoy never deletes from a customer bucket: the cloud codes are final retained reports."""
    for code in (r.ReasonCode.CLOUD_OBJECT_UNFINISHED, r.ReasonCode.CLOUD_OBJECT_ABANDONED):
        assert code.family == "retain" and r.REASON_SUMMARY[code]
        item = r.ReportItem(ID_A, r.ArtifactClass.PUBLISHED_OUTPUT, r.Decision.RETAINED, code)
        assert item.reason is code
        assert r.ReasonCode.parse(code.value) is code
