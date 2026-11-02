"""A1: the canary report types hold closed enums, bools, ints and tuples of those only (F34 I1)."""

from __future__ import annotations

import dataclasses
import enum
import pickle
import typing

import pytest

from decoy_engine.verification.canary import (
    CanaryArtifactOutcome,
    CanaryArtifactReadFailure,
    CanaryBindingSource,
    CanaryColumnFinding,
    CanaryMatchMode,
    CanaryNormalizationSchema,
    CanaryPolicyInputError,
    CanaryReason,
    CanarySpanPolicy,
    CanarySpanReport,
    CanarySpanReportInvalid,
    CanarySpanReportSchema,
    CanaryStatus,
)

MARKER = "CANARY-MARKER-7f3a-ssn-123-45-6789"
MAX_COUNT = 2**53 - 1

FINDING_FIELDS = (
    "artifact_ordinal",
    "column_ordinal",
    "match_mode",
    "affected_cell_count",
    "count_capped",
)
REPORT_FIELDS = (
    "schema_version",
    "normalization_policy_version",
    "status",
    "complete",
    "reasons",
    "binding_source",
    "input_canary_count",
    "accepted_canary_count",
    "exact_pattern_count",
    "normalized_pattern_count",
    "normalization_collision_count",
    "excluded_short_canary_count",
    "requested_artifact_count",
    "scanned_artifact_count",
    "duplicate_artifact_count",
    "unsupported_artifact_count",
    "unreadable_artifact_count",
    "resource_limited_artifact_count",
    "mismatched_artifact_count",
    "unreported_artifact_count",
    "scanned_column_count",
    "scanned_row_count",
    "scanned_text_byte_count",
    "oversized_cell_count",
    "uncompared_cell_count",
    "affected_cell_count",
    "finding_count",
    "count_capped",
    "findings",
)
INT_FIELDS = tuple(n for n in REPORT_FIELDS if n.endswith("_count") and n not in ("count_capped",))


def _field_type_violations(cls: type) -> list[str]:
    """Fields whose type is anything but int, bool, a canary enum or a tuple of those."""
    hints = typing.get_type_hints(cls)
    bad = []
    for f in dataclasses.fields(cls):
        t = hints[f.name]
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            bad.append(f"{f.name}: default")
        if typing.get_origin(t) is tuple:
            args = typing.get_args(t)
            if len(args) != 2 or args[1] is not Ellipsis:
                bad.append(f"{f.name}: {t}")
                continue
            t = args[0]
        ok = t in (int, bool) or (
            isinstance(t, type)
            and (issubclass(t, enum.Enum) or t is CanaryColumnFinding)
            and t.__module__.startswith("decoy_engine.verification.canary")
            and not issubclass(t, str)
        )
        if not ok:
            bad.append(f"{f.name}: {t}")
    return bad


def _finding(**over: object) -> CanaryColumnFinding:
    kw: dict[str, object] = {
        "artifact_ordinal": 0,
        "column_ordinal": 1,
        "match_mode": CanaryMatchMode.EXACT_SPAN,
        "affected_cell_count": 3,
        "count_capped": False,
    }
    kw.update(over)
    return CanaryColumnFinding(**kw)  # type: ignore[arg-type]


def _report(**over: object) -> CanarySpanReport:
    kw: dict[str, object] = {n: 0 for n in INT_FIELDS}
    kw.update(
        schema_version=CanarySpanReportSchema.V1,
        normalization_policy_version=CanaryNormalizationSchema.V1,
        status=CanaryStatus.INCOMPLETE,
        complete=False,
        reasons=(CanaryReason.MATCH, CanaryReason.SCAN_INCOMPLETE, CanaryReason.RESOURCE_LIMIT),
        binding_source=CanaryBindingSource.PUBLICATION_RECORD,
        requested_artifact_count=2,
        scanned_artifact_count=1,
        resource_limited_artifact_count=1,
        affected_cell_count=3,
        finding_count=1,
        count_capped=False,
        findings=(_finding(),),
    )
    kw.update(over)
    return CanarySpanReport(**kw)  # type: ignore[arg-type]


def test_types_have_exact_fields_5_29() -> None:
    assert tuple(f.name for f in dataclasses.fields(CanaryColumnFinding)) == FINDING_FIELDS
    assert tuple(f.name for f in dataclasses.fields(CanarySpanReport)) == REPORT_FIELDS
    assert len(FINDING_FIELDS) == 5 and len(REPORT_FIELDS) == 29
    for cls, instance in ((CanaryColumnFinding, _finding()), (CanarySpanReport, _report())):
        params = cls.__dataclass_params__  # type: ignore[attr-defined]
        assert params.frozen and "__slots__" in vars(cls) and not hasattr(instance, "__dict__")
        assert _field_type_violations(cls) == []

    # Seeded violation: the field walk catches a value-bearing field.
    @dataclasses.dataclass(frozen=True, slots=True)
    class Stub:
        status: CanaryStatus
        message: str

    assert _field_type_violations(Stub) == ["message: <class 'str'>"]


def test_repr_str_pickle_asdict_carry_no_marker() -> None:
    report = _report()
    surfaces = [repr(report), str(report), repr(dataclasses.asdict(report))]
    blob = pickle.dumps(report)
    assert pickle.loads(blob) == report  # noqa: S301 (bytes this test just produced)
    assert all(MARKER not in s for s in surfaces) and MARKER.encode() not in blob

    # No field accepts the marker, a bool for an int, or a count past 2**53-1; the refusal is fixed.
    for name in REPORT_FIELDS:
        for bad in (MARKER, MARKER.encode(), (MARKER,)):
            with pytest.raises(CanarySpanReportInvalid) as info:
                _report(**{name: bad})
            assert info.value.args == () and str(info.value) == ""
            assert MARKER not in repr(info.value)
    for name in INT_FIELDS:
        for bad in (True, MAX_COUNT + 1, -1, 1.0):
            with pytest.raises(CanarySpanReportInvalid):
                _report(**{name: bad})
    for name in FINDING_FIELDS:
        with pytest.raises(CanarySpanReportInvalid):
            _finding(**{name: MARKER})
    for bad in (True, MAX_COUNT + 1, -1):
        with pytest.raises(CanarySpanReportInvalid):
            _finding(affected_cell_count=bad)
    assert _finding(affected_cell_count=MAX_COUNT).affected_cell_count == MAX_COUNT

    failure = CanaryArtifactReadFailure(CanaryArtifactOutcome.UNREADABLE)
    assert failure.args == (CanaryArtifactOutcome.UNREADABLE,) and str(failure) != MARKER
    for bad_outcome in (MARKER, CanaryArtifactOutcome.SCANNED, "unreadable"):
        with pytest.raises(CanaryPolicyInputError) as perr:
            CanaryArtifactReadFailure(bad_outcome)  # type: ignore[arg-type]
        assert perr.value.args == ()


def test_report_rejects_bad_reasons_and_complete_mismatch() -> None:
    out_of_order = (CanaryReason.SCAN_INCOMPLETE, CanaryReason.MATCH, CanaryReason.RESOURCE_LIMIT)
    bad_cases: list[dict[str, object]] = [
        {"reasons": out_of_order},
        {"reasons": (CanaryReason.MATCH, CanaryReason.MATCH, CanaryReason.SCAN_INCOMPLETE)},
        {"reasons": [CanaryReason.MATCH, CanaryReason.SCAN_INCOMPLETE]},
        {"reasons": ("canary_match", "canary_scan_incomplete")},
        {"complete": True},
        {"status": CanaryStatus.COMPLETE, "complete": False, "reasons": (CanaryReason.MATCH,)},
        # canary_scan_incomplete if and only if INCOMPLETE (I6)
        {"reasons": (CanaryReason.MATCH, CanaryReason.RESOURCE_LIMIT)},
        {"status": CanaryStatus.COMPLETE, "complete": True},
        # REJECTED carries exactly (canary_input_rejected,)
        {
            "status": CanaryStatus.REJECTED,
            "reasons": (CanaryReason.INPUT_REJECTED,),
            "complete": True,
        },
        {
            "status": CanaryStatus.REJECTED,
            "reasons": (CanaryReason.MATCH, CanaryReason.INPUT_REJECTED),
        },
        # findings sorted, unique, bounded by finding_count
        {"findings": (_finding(column_ordinal=2), _finding(column_ordinal=1)), "finding_count": 2},
        {"findings": (_finding(), _finding()), "finding_count": 2},
        {"findings": (_finding(), _finding(column_ordinal=2)), "finding_count": 1},
        {"findings": ((_finding(),),)},
        {"findings": (_finding(),), "finding_count": 2, "count_capped": False},
        {"status": "incomplete"},
        {"complete": 0},
    ]
    for case in bad_cases:
        with pytest.raises(CanarySpanReportInvalid):
            _report(**case)
    ok = _report(
        status=CanaryStatus.COMPLETE,
        complete=True,
        reasons=(CanaryReason.MATCH,),
        resource_limited_artifact_count=0,
        scanned_artifact_count=2,
        finding_count=2,
        count_capped=True,
    )
    assert ok.complete and ok.reasons == (CanaryReason.MATCH,)
    rejected = _report(
        status=CanaryStatus.REJECTED,
        reasons=(CanaryReason.INPUT_REJECTED,),
        finding_count=0,
        findings=(),
    )
    assert rejected.reasons == (CanaryReason.INPUT_REJECTED,)


def test_policy_input_types_are_exact() -> None:
    assert CanarySpanPolicy.default() == CanarySpanPolicy(8, 100_000, 256 * 1024 * 1024)
    for bad in ((True, 1, 1), (8, 1.0, 1), (8, 1, "1")):
        with pytest.raises(CanaryPolicyInputError):
            CanarySpanPolicy(*bad)  # type: ignore[arg-type]
