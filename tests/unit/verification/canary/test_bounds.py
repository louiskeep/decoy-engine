"""A20: every input limit at below, equal and above; plus the wall time at the default budget."""

from __future__ import annotations

import time

import pyarrow as pa
import pytest

from decoy_engine.verification.canary import (
    CanaryArtifactInput,
    CanaryBindingSource,
    CanaryConsumerReason,
    CanarySpanPolicy,
    CanaryStatus,
    scan_canary_artifacts,
)
from decoy_engine.verification.canary import (
    CanaryReason as R,
)
from decoy_engine.verification.canary._match import MAX_CELL_UTF8_BYTES
from tests.unit.verification.canary.test_scan_end_to_end import (
    Spy,
    art,
    batch,
    scan,
    strings,
)

MIB = 1024 * 1024


def names(n: int) -> list[str]:
    return [f"canary-{i:05d}" for i in range(n)]


def full_size(n_bytes: int) -> list[str]:  # distinct canaries of 4,096 bytes each
    return [f"{i:04d}" + "a" * 4092 for i in range(n_bytes // 4096)]


def pol(**kw: int) -> CanarySpanPolicy:
    base = {"span_min_scalars": 8, "max_rows": 100_000, "max_text_bytes": 2**28}
    return CanarySpanPolicy(**{**base, **kw})


# id: (canaries, policy, rejected)
EDGES = {
    "canary_count-zero": ([], pol(), True),
    "canary_count-one": (names(1), pol(), False),
    "canary_count-below": (names(4095), pol(), False),
    "canary_count-equal": (names(4096), pol(), False),
    "canary_count-above": (names(4097), pol(), True),
    "canary_bytes-empty": ([""], pol(), True),
    "canary_bytes-below": (["a" * 4095], pol(), False),
    "canary_bytes-equal": (["a" * 4096], pol(), False),
    "canary_bytes-above": (["a" * 4097], pol(), True),
    "canary_bytes-multibyte-equal": (["é" * 2048], pol(), False),
    "canary_bytes-multibyte-above": (["é" * 2049], pol(), True),
    "total_bytes-below": (full_size(MIB - 4096), pol(), False),
    "total_bytes-equal": (full_size(MIB), pol(), False),
    "total_bytes-above": ([*full_size(MIB), "z"], pol(), True),
    "span_min-below": (names(1), pol(span_min_scalars=3), True),
    "span_min-equal-low": (names(1), pol(span_min_scalars=4), False),
    "span_min-equal-high": (names(1), pol(span_min_scalars=128), False),
    "span_min-above": (names(1), pol(span_min_scalars=129), True),
    "max_rows-below": (names(1), pol(max_rows=0), True),
    "max_rows-equal-low": (names(1), pol(max_rows=1), False),
    "max_rows-equal-high": (names(1), pol(max_rows=5_000_000), False),
    "max_rows-above": (names(1), pol(max_rows=5_000_001), True),
    "max_text-below": (names(1), pol(max_text_bytes=0), True),
    "max_text-equal-low": (names(1), pol(max_text_bytes=1), False),
    "max_text-equal-high": (names(1), pol(max_text_bytes=2 * 1024**3), False),
    "max_text-above": (names(1), pol(max_text_bytes=2 * 1024**3 + 1), True),
}


@pytest.mark.parametrize("edge", sorted(EDGES))
def test_limit_edges(edge: str) -> None:
    canaries, policy, rejected = EDGES[edge]
    spy = Spy()
    report = scan(canaries, [CanaryArtifactInput(0, None, spy.opener())], policy=policy)
    if rejected:
        assert report.status is CanaryStatus.REJECTED and report.reasons == (R.INPUT_REJECTED,)
        assert spy.opened == 0
    else:
        assert report.status is CanaryStatus.COMPLETE and spy.opened == 1
        assert report.accepted_canary_count == len(set(canaries))


@pytest.mark.parametrize(("columns", "limited"), [(1024, False), (1025, True)])
def test_limit_edges_columns(columns: int, limited: bool) -> None:
    report = scan(names(1), [art(0, batch(*[pa.array([1])] * columns))])
    assert report.resource_limited_artifact_count == limited
    assert report.scanned_column_count == (0 if limited else columns)
    assert (R.RESOURCE_LIMIT in report.reasons) is limited


@pytest.mark.parametrize(("extra", "oversized"), [(0, False), (1, True)])
def test_limit_edges_cell_one_mib(extra: int, oversized: bool) -> None:
    assert MAX_CELL_UTF8_BYTES == MIB
    cell = "x" * (MIB - 8 + extra) + "ABCDEFGH"
    report = scan(["ABCDEFGH"], [art(0, strings(cell))])
    assert report.oversized_cell_count == oversized
    assert report.finding_count == (0 if oversized else 1)
    assert (R.RESOURCE_LIMIT in report.reasons) is oversized


def test_duplicates_are_counted_but_not_accepted() -> None:
    report = scan(["canary-value-1", "canary-value-1", "canary-value-2"], [])
    assert (report.input_canary_count, report.accepted_canary_count) == (3, 2)


BAD_CANARIES = {
    "bare_str": "canary-value-1",
    "non_str": ["canary-value-1", 7],
    "str_subclass": [type("S", (str,), {})("canary-value-1")],
    "lone_surrogate": ["canary-\ud800-value"],
    "whitespace_only": [" \t　"],
    "post_13_0_mark": ["xxxxxxé᫁"],
}


@pytest.mark.parametrize("name", sorted(BAD_CANARIES))
def test_invalid_canaries_are_rejected_not_raised(name: str) -> None:
    spy = Spy()
    report = scan(BAD_CANARIES[name], [CanaryArtifactInput(0, None, spy.opener())])
    assert report.status is CanaryStatus.REJECTED and spy.opened == 0


BAD_CALLS = {
    "ordinal_gap": [art(0), art(2)],
    "ordinal_duplicate": [art(0), art(0)],
    "artifact_wrong_type": ["not an artifact"],
}


@pytest.mark.parametrize("name", sorted(BAD_CALLS))
def test_invalid_artifact_lists_are_rejected(name: str) -> None:
    assert scan(names(1), BAD_CALLS[name]).status is CanaryStatus.REJECTED


@pytest.mark.parametrize(
    "kw",
    [
        {"policy": "default"},
        {"binding_source": "none"},
        {"consumer_reasons": {CanaryConsumerReason.PUBLICATION_INACTIVE}},
        {"consumer_reasons": frozenset({"publication_inactive"})},
    ],
)
def test_invalid_call_arguments_are_rejected(kw: dict[str, object]) -> None:
    args = {
        "policy": CanarySpanPolicy.default(),
        "binding_source": CanaryBindingSource.PUBLICATION_RECORD,
        "artifacts": [],
        "consumer_reasons": frozenset(),
    }
    report = scan_canary_artifacts(names(1), **{**args, **kw})  # type: ignore[arg-type]
    assert report.status is CanaryStatus.REJECTED


def test_wall_time_at_default_budget(record_property: pytest.RecordProperty) -> None:
    chunk = strings(*(f"row {i:06d} " + "lorem ipsum " * 5 for i in range(1000)))
    artifact = CanaryArtifactInput(0, None, lambda: iter([chunk] * 100))
    start = time.perf_counter()
    report = scan(["canary-never-present"], [artifact])
    elapsed = time.perf_counter() - start
    record_property("wall_seconds", f"{elapsed:.2f}")
    assert report.scanned_row_count == 100_000 and report.status is CanaryStatus.COMPLETE
