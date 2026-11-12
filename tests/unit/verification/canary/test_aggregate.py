"""A9 (buckets, budgets, iterator cleanup, saturation) and E0g (the report cross-check)."""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.verification.canary import (
    MAX_COUNT,
    CanaryArtifactInput,
    CanaryArtifactReadFailure,
    CanaryBindingSource,
    CanaryConsumerReason,
    CanarySpanPolicy,
    CanarySpanReport,
    CanarySpanReportInvalid,
    CanaryStatus,
)
from decoy_engine.verification.canary import (
    CanaryArtifactOutcome as O,
)
from decoy_engine.verification.canary import (
    CanaryReason as R,
)
from decoy_engine.verification.canary._aggregate import Totals, build_report, check_report
from decoy_engine.verification.canary._match import CellTally
from tests.unit.verification.canary.test_report_fields import _finding, _report
from tests.unit.verification.canary.test_scan_end_to_end import (
    PUBLISHED,
    Spy,
    art,
    batch,
    never_opened,
    pre,
    scan,
    strings,
)

BUCKETS = ("scanned", "duplicate", "unsupported", "unreadable", "resource_limited", "mismatched")
CANARY = ("canary-value-1",)


def buckets(report: CanarySpanReport) -> dict[str, int]:
    names = (*BUCKETS, "unreported")
    return {n: v for n in names if (v := getattr(report, f"{n}_artifact_count"))}


def failing(ordinal: int, outcome: O) -> CanaryArtifactInput:
    spy = Spy([strings("x")], CanaryArtifactReadFailure(outcome))
    return CanaryArtifactInput(ordinal, None, spy.opener())


BUCKET_CASES = {
    "none": ([], {}, (R.SCAN_INCOMPLETE, R.TARGET_UNAVAILABLE)),
    "one_scanned": ([art(0, strings("a"))], {"scanned": 1}, ()),
    "preclassified": (
        [pre(0, O.DUPLICATE), pre(1, O.UNSUPPORTED), pre(2, O.UNREADABLE), pre(3, O.MISMATCHED)],
        {"duplicate": 1, "unsupported": 1, "unreadable": 1, "mismatched": 1},
        (R.SCAN_INCOMPLETE, R.PUBLICATION_MISMATCH, R.ARTIFACT_UNREADABLE, R.ARTIFACT_UNSUPPORTED),
    ),
    "read_failures": (
        [failing(0, O.UNREADABLE), failing(1, O.UNSUPPORTED), failing(2, O.RESOURCE_LIMITED)],
        {"unreadable": 1, "unsupported": 1, "resource_limited": 1},
        (R.SCAN_INCOMPLETE, R.ARTIFACT_UNREADABLE, R.ARTIFACT_UNSUPPORTED, R.RESOURCE_LIMIT),
    ),
}


@pytest.mark.parametrize("case", sorted(BUCKET_CASES))
def test_buckets_sum_to_requested(case: str) -> None:
    artifacts, expected, reasons = BUCKET_CASES[case]
    report = scan(CANARY, artifacts)
    assert buckets(report) == expected
    assert report.requested_artifact_count == len(artifacts) == sum(expected.values())
    assert report.reasons == reasons


@pytest.mark.parametrize("n", [65, 100, 200])
def test_artifacts_past_64_resource_limited(n: int) -> None:
    artifacts = [art(i) for i in range(64)] + [never_opened(i) for i in range(64, n)]
    report = scan(CANARY, artifacts)
    assert buckets(report) == {"scanned": 64, "resource_limited": n - 64}
    assert report.requested_artifact_count == n and report.status is CanaryStatus.INCOMPLETE
    assert R.RESOURCE_LIMIT in report.reasons


def test_budget_stop_marks_resource_limited_then_unreported() -> None:
    full = strings("a", "b")
    artifacts = [art(0, full, full, full), never_opened(1), pre(2, O.DUPLICATE), never_opened(3)]
    report = scan(CANARY, artifacts, policy=CanarySpanPolicy(8, 4, 2**28))
    assert report.scanned_row_count == 4
    assert buckets(report) == {"resource_limited": 1, "duplicate": 1, "unreported": 2}
    assert report.reasons == (R.SCAN_INCOMPLETE, R.RESOURCE_LIMIT)


def test_text_budget_stops_before_next_batch() -> None:
    chunk = strings("abcdefgh")
    report = scan(
        CANARY, [art(0, chunk, chunk), never_opened(1)], policy=CanarySpanPolicy(8, 99, 8)
    )
    assert report.scanned_text_byte_count == 8
    assert buckets(report) == {"resource_limited": 1, "unreported": 1}


def test_row_budget_met_exactly_at_eof_is_scanned() -> None:
    report = scan(CANARY, [art(0, strings("a", "b"))], policy=CanarySpanPolicy(8, 2, 2**28))
    assert buckets(report) == {"scanned": 1} and report.status is CanaryStatus.COMPLETE


def wide(n: int) -> pa.RecordBatch:
    return batch(*[pa.array([1])] * n)


EXITS = {  # name: (spy items, exception raised by the iterator, max_rows, raised from scan)
    "eof": ([strings("a")], None, 99, None),
    "budget": ([strings("a"), strings("b")], None, 1, None),
    "read_failure": ([strings("a")], CanaryArtifactReadFailure(O.UNREADABLE), 99, None),
    "unsupported_schema": ([batch(pa.array([[1]]))], None, 99, None),
    "too_wide": ([wide(1025)], None, 99, None),
    "foreign_exception": ([strings("a")], RuntimeError("boom"), 99, RuntimeError),
    "keyboard_interrupt": ([strings("a")], KeyboardInterrupt(), 99, KeyboardInterrupt),
    "not_a_batch": (["not a batch"], None, 99, Exception),
}


@pytest.mark.parametrize("exit_name", sorted(EXITS))
def test_iterator_closed_on_each_exit(exit_name: str) -> None:
    items, raises, max_rows, propagated = EXITS[exit_name]
    spy = Spy(items, raises)
    artifacts = [CanaryArtifactInput(0, None, spy.opener())]
    policy = CanarySpanPolicy(8, max_rows, 2**28)
    if propagated is not None:
        with pytest.raises(propagated):
            scan(CANARY, artifacts, policy=policy)
    else:
        report = scan(CANARY, artifacts, policy=policy)
        assert report.requested_artifact_count == sum(buckets(report).values())
    assert spy.opened == 1 and spy.closed == 1


def test_generator_is_finished_after_budget_stop() -> None:
    state = {}

    def open_batches():  # type: ignore[no-untyped-def]
        state["gen"] = gen = (strings("a") for _ in range(5))
        return gen

    scan(CANARY, [CanaryArtifactInput(0, None, open_batches)], policy=CanarySpanPolicy(8, 1, 2**28))
    assert state["gen"].gi_frame is None


def test_count_saturation() -> None:
    totals = Totals()
    totals.rows(MAX_COUNT - 1)
    totals.rows(5)
    tally = CellTally(MAX_COUNT, 0, 0, 0, 0, 0)
    totals.cells(0, 0, tally, uncompared=0, text_bytes=0)
    totals.cells(0, 0, tally, uncompared=0, text_bytes=0)
    totals.outcome(O.UNREADABLE)
    report = build_report(
        binding_source=PUBLISHED,
        input_canary_count=1,
        accepted_canary_count=1,
        patterns=None,
        requested_artifact_count=1,
        totals=totals,
    )
    assert report.scanned_row_count == MAX_COUNT and report.affected_cell_count == MAX_COUNT
    assert report.findings[0].affected_cell_count == MAX_COUNT and report.findings[0].count_capped
    assert report.count_capped


def test_more_than_4096_findings_keep_the_first_in_order() -> None:
    totals = Totals()
    tally = CellTally(1, 0, 0, 0, 0, 0)
    for column in range(4100):
        totals.cells(0, column, tally, uncompared=0, text_bytes=0)
    totals.outcome(O.SCANNED)
    report = build_report(
        binding_source=PUBLISHED,
        input_canary_count=1,
        accepted_canary_count=1,
        patterns=None,
        requested_artifact_count=1,
        totals=totals,
    )
    assert report.finding_count == 4100 and len(report.findings) == 4096 and report.count_capped
    assert report.affected_cell_count == 4100
    assert [f.column_ordinal for f in report.findings] == list(range(4096))


def forged(shape: str) -> CanarySpanReport:
    done = {"status": CanaryStatus.COMPLETE, "complete": True}
    one = {"requested_artifact_count": 1, "scanned_artifact_count": 1}
    if shape == "a":  # COMPLETE with an unreadable artifact
        kw = {**done, "reasons": (R.ARTIFACT_UNREADABLE,), "requested_artifact_count": 1}
        kw.update(scanned_artifact_count=0, unreadable_artifact_count=1)
    elif shape == "b":  # COMPLETE without a binding
        kw = {**done, **one, "reasons": (), "binding_source": CanaryBindingSource.NONE}
    elif shape == "c":  # canary_match without a finding
        kw = {**done, **one, "reasons": (R.MATCH,), "finding_count": 0, "findings": ()}
    elif shape == "d":  # input rejected inside an INCOMPLETE report
        kw = {"reasons": (R.SCAN_INCOMPLETE, R.INPUT_REJECTED, R.PUBLICATION_MISMATCH)}
        kw.update(requested_artifact_count=1, scanned_artifact_count=0, mismatched_artifact_count=1)
        kw.update(finding_count=0, findings=(), affected_cell_count=0)
    else:  # REJECTED carrying findings
        kw = {"status": CanaryStatus.REJECTED, "complete": False, "reasons": (R.INPUT_REJECTED,)}
        kw.update(
            requested_artifact_count=0, scanned_artifact_count=0, resource_limited_artifact_count=0
        )
        kw.update(finding_count=3, count_capped=True, findings=(), affected_cell_count=0)
    if shape in "abc":
        kw.update(finding_count=kw.get("finding_count", 0), findings=(), affected_cell_count=0)
        kw.update(resource_limited_artifact_count=0)
    return _report(**kw)


@pytest.mark.parametrize("shape", list("abcde"))
def test_forged_report_refused(shape: str) -> None:
    report = forged(shape)  # E0 accepts it, so the case is not vacuous
    assert type(report) is CanarySpanReport
    with pytest.raises(CanarySpanReportInvalid):
        check_report(report)


def test_rejected_report_with_a_finding_is_refused() -> None:
    kw = {"status": CanaryStatus.REJECTED, "complete": False, "reasons": (R.INPUT_REJECTED,)}
    kw.update(
        requested_artifact_count=0, scanned_artifact_count=0, resource_limited_artifact_count=0
    )
    with pytest.raises(CanarySpanReportInvalid):
        check_report(_report(**kw))  # default _report carries one finding


def test_scan_reports_pass_cross_check() -> None:
    reports = [
        scan(CANARY, [art(0, strings("canary-value-1"))]),
        scan(CANARY, [art(0, strings("x"))], binding=CanaryBindingSource.NONE),
        scan(CANARY, [], consumer=frozenset(CanaryConsumerReason)),
        scan([], [art(0)]),
    ]
    for report in reports:
        check_report(report)
    assert reports[3].status is CanaryStatus.REJECTED
    assert reports[0].finding_count == 1 and reports[0].findings == (
        _finding(
            column_ordinal=0, match_mode=reports[0].findings[0].match_mode, affected_cell_count=1
        ),
    )
