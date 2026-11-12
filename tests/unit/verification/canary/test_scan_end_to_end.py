"""A5r, A7r and the E2 errata guards: the E1 and E2 cases re-run through ``scan_canary_artifacts``.

Also holds the helpers the other E3 test files share. ``scan`` cross-checks every report it
returns with ``check_report``, so each test in the E3 files exercises the E0g guard too.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Sequence

import pyarrow as pa
import pytest

from decoy_engine.verification.canary import (
    CanaryArtifactInput,
    CanaryArtifactOutcome,
    CanaryBindingSource,
    CanaryCompletionClass,
    CanaryConsumerReason,
    CanaryReason,
    CanarySpanPolicy,
    CanarySpanReport,
    CanaryStatus,
    completion_class,
    scan_canary_artifacts,
)
from decoy_engine.verification.canary._aggregate import check_report
from tests.unit.verification.canary.test_canonical_text import CASES, CLASS_CODES, REASON_CODES
from tests.unit.verification.canary.test_matching import MODE_MATRIX

PUBLISHED = CanaryBindingSource.PUBLICATION_RECORD
R = CanaryReason


class Spy:
    """A batch iterator that counts opens and closes (it is not a generator)."""

    def __init__(self, items: Iterable[object] = (), raises: BaseException | None = None) -> None:
        self.items, self.raises, self.opened, self.closed = list(items), raises, 0, 0

    def opener(self) -> Callable[[], Spy]:
        def open_batches() -> Spy:
            self.opened += 1
            return self

        return open_batches

    def __iter__(self) -> Spy:
        return self

    def __next__(self) -> object:
        if self.items:
            return self.items.pop(0)
        if self.raises is not None:
            raise self.raises
        raise StopIteration

    def close(self) -> None:
        self.closed += 1


def batch(*columns: pa.Array) -> pa.RecordBatch:
    return pa.RecordBatch.from_arrays(list(columns), names=[f"c{i}" for i in range(len(columns))])


def strings(*cells: str | None) -> pa.RecordBatch:
    return batch(pa.array(list(cells), pa.string()))


def art(ordinal: int, *batches: pa.RecordBatch) -> CanaryArtifactInput:
    return CanaryArtifactInput(ordinal, None, lambda: iter(batches))


def pre(ordinal: int, outcome: CanaryArtifactOutcome) -> CanaryArtifactInput:
    return CanaryArtifactInput(ordinal, preclassified=outcome, open_batches=None)


def never_opened(ordinal: int) -> CanaryArtifactInput:
    def open_batches() -> Iterable[pa.RecordBatch]:
        raise AssertionError("artifact opened")

    return CanaryArtifactInput(ordinal, None, open_batches)  # type: ignore[arg-type]


def scan(
    canaries: Sequence[str],
    artifacts: Sequence[CanaryArtifactInput],
    *,
    policy: CanarySpanPolicy | None = None,
    binding: CanaryBindingSource = PUBLISHED,
    consumer: frozenset[CanaryConsumerReason] = frozenset(),
) -> CanarySpanReport:
    report = scan_canary_artifacts(
        canaries,
        policy or CanarySpanPolicy.default(),
        binding_source=binding,
        artifacts=artifacts,
        consumer_reasons=consumer,
    )
    check_report(report)
    return report


A5R = [
    "5b_contiguous_span",
    "5c_decomposed_cell_composed_canary",
    "5f_short_canary_exact_cell_only",
    "5i_canary_with_post_13_0_mark_rejected",
    "5j_cell_with_post_13_0_mark_uncompared",
    "5k_whitespace_canary_short_after_normalization",
]


@pytest.mark.parametrize("name", A5R)
def test_mode_cases_through_scan(name: str) -> None:
    case = MODE_MATRIX[name]
    spy = Spy([strings(*case.cells)])
    policy = CanarySpanPolicy(case.span_min, 100_000, 2**28)
    report = scan(case.canaries, [CanaryArtifactInput(0, None, spy.opener())], policy=policy)
    assert report.status is case.status and report.reasons == case.reasons
    if case.status is CanaryStatus.REJECTED:
        assert spy.opened == 0 and report.finding_count == 0 and report.accepted_canary_count == 0
        return
    modes = [case.tally.get(m, 0) for m in ("exact_cell", "exact_span", "nfc_whitespace_span")]
    assert report.finding_count == sum(n > 0 for n in modes)
    assert report.affected_cell_count == sum(modes)
    assert report.excluded_short_canary_count == case.short
    assert report.uncompared_cell_count == case.tally.get("uncompared", 0)
    assert spy.opened == spy.closed == 1


A7R = [
    "7b_float64_integral",
    "7g_timestamp_ns_midnight_span",
    "7k_list_unsupported",
    "7n_tokyo_zone_unsupported",
    "7p_string",
    "7p_large_string",
    "7p_string_view",
    "7p_dictionary_int32_string",
]


@pytest.mark.parametrize("name", A7R)
def test_typed_cases_through_scan(name: str) -> None:
    case = CASES[name]
    report = scan((case.canary,), [art(0, batch(case.build()))])  # no exception on bad UTF-8
    assert report.reasons == tuple(REASON_CODES[c] for c in case.reasons)
    assert completion_class(report) is CLASS_CODES[case.klass]
    assert report.finding_count == case.findings
    assert report.uncompared_cell_count == case.uncompared
    assert report.unsupported_artifact_count == (case.cells is None)
    if case.uncompared or case.cells is None:
        assert completion_class(report) is not CanaryCompletionClass.CLEAR
        assert not report.complete


EPOCH = dt.datetime(1970, 1, 1)
DOB_DAYS = (dt.datetime(1980, 2, 14) - EPOCH).days
DOB_US = DOB_DAYS * 86_400 * 10**6
TS_UTC = pa.timestamp("us", tz="UTC")
# Each array ends with the canary row, so reading from offset 0 instead of the slice offset
# misses it; only decoys sit in the sliced-out and leading rows.
SLICED = {
    "bool": (pa.array([False, False, True]), "true"),
    "int16": (pa.array([1, 2, 31337], pa.int16()), "31337"),
    "timestamp_us_utc": (pa.array([0, 86_400 * 10**6, DOB_US]).view(TS_UTC), "1980-02-14T00:00:00"),
    "date32": (pa.array([1, 2, DOB_DAYS], pa.int32()).view(pa.date32()), "1980-02-14"),
    "string": (pa.array(["aaaa", "bbbb", "canary-1"]), "canary-1"),
    "dictionary": (
        pa.DictionaryArray.from_arrays(pa.array([0, 0, 1], pa.int32()), pa.array(["xxxx", "canary-1"])),
        "canary-1",
    ),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(SLICED))
def test_sliced_array_reads_from_its_offset(name: str) -> None:
    values, canary = SLICED[name]
    found = scan((canary,), [art(0, batch(values.slice(1)))])
    assert completion_class(found) is CanaryCompletionClass.FINDINGS
    assert found.finding_count == 1 and found.affected_cell_count == 1
    decoys = scan((canary,), [art(0, batch(values.slice(0, 2)))])  # non-vacuity: no canary row
    assert completion_class(decoys) is CanaryCompletionClass.CLEAR


def test_dictionary_index_bounds_make_cells_uncompared() -> None:
    indices = pa.array([0, 5, -1, None], pa.int32())
    column = pa.DictionaryArray.from_arrays(indices, pa.array(["a"]), safe=False)
    report = scan(("a",), [art(0, batch(column))])
    assert report.finding_count == 1 and report.affected_cell_count == 1
    assert report.uncompared_cell_count == 2
    assert report.reasons == (R.MATCH, R.SCAN_INCOMPLETE, R.CELL_UNCOMPARED)
