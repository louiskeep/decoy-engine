"""C2 acceptance: bucket_perturb on the chunked route, parity and output type.

Native chunked output must equal the pandas-oracle chunked output (values, Arrow field
type, metadata) across chunk shapes, chunk sizes, buckets and thread counts. Unlike
categorical, the output type is NOT pinned to string: it follows the oracle's
content-dependent rule (an empty or all-null chunk is Arrow `null`, any chunk with a
non-null value is `string`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._chunked import concat_masked_chunks
from tests.native._b8_support import identical
from tests.native._chunked_bucket_perturb_support import (
    FORCE,
    assert_same_as_oracle,
    bp_col,
    date_value,
    expected_derive_sizes,
    make_config,
    passthrough,
    run_one,
    run_pair,
    source,
    spy_index_kernel,
    with_force,
)
from tests.native._chunked_entry_support import (
    NEEDS_COMPANION,
    TABLE,
    column_values,
    force_oracle,
    forced_reason,
    split,
)
from tests.native.test_chunked_entry_values_schema import _full_frame

_VALUED = [date_value(i) for i in range(23)]
_RAGGED: list[str | None] = [None if i % 6 == 2 else v for i, v in enumerate(_VALUED)]

# Content shapes. Chunk size then decides where the boundaries fall inside each one.
_SHAPES: dict[str, list[str | None]] = {
    "all_null": [None] * 7,
    "single_row": [date_value(3)],
    "ragged": _RAGGED,
    "unparseable_mixed": [date_value(1), "not-a-date", None, "2021-13-45", date_value(2), ""],
    "null_block_then_valued": [None] * 7 + [date_value(i) for i in range(9)],
    "valued_then_null_block": [date_value(i) for i in range(9)] + [None] * 7,
}


def _chunks(shape: str, size: int) -> list[pa.Table]:
    return split(source(_SHAPES[shape]), size)


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("size", [1, 7, 50_000])
@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_native_chunked_equals_oracle_chunked(
    shape: str, bucket: str, size: int, threads: int
) -> None:
    columns = [bp_col(bucket=bucket), passthrough("p")]
    native, forced = run_pair(columns, _chunks(shape, size), native_threads=threads)
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
def test_an_empty_chunk_is_identical_on_both_legs(bucket: str, threads: int) -> None:
    native, forced = run_pair(
        [bp_col(bucket=bucket), passthrough("p")], [source([])], native_threads=threads
    )
    assert_same_as_oracle(native, forced)
    assert native.out[0].num_rows == 0


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
def test_empty_and_all_null_chunks_between_valued_chunks_are_identical(threads: int) -> None:
    chunks = [
        source([date_value(i) for i in range(4)]),
        source([]),
        source([None, None, None]),
        source([date_value(i) for i in range(4, 9)]),
        source([]),
    ]
    native, forced = run_pair([bp_col(), passthrough("p")], chunks, native_threads=threads)
    assert_same_as_oracle(native, forced)


def _outcome(columns: list[dict[str, Any]], chunks: list[pa.Table]) -> tuple[Any, list[Any]]:
    """The tables a run yields, or the (type, code) of the error it raises, plus the
    caller-owned route evidence (kept even when the run raises)."""
    evidence: list[Any] = []
    try:
        return run_one(make_config(columns), chunks, route_evidence_sink=evidence).out, evidence
    except Exception as exc:
        return (type(exc).__name__, getattr(exc, "code", None)), evidence


@NEEDS_COMPANION
@pytest.mark.parametrize(
    "later",
    [
        pa.table({"d": pa.nulls(3), "p": pa.array([7, 8, 9], pa.int64())}),
        pa.table({"d": pa.array([1, 2, 3], pa.int64()), "p": pa.array([7, 8, 9], pa.int64())}),
    ],
    ids=["null_typed", "int64"],
)
def test_a_drifted_later_chunk_has_the_same_outcome_on_both_legs(later: pa.Table) -> None:
    valued = source([date_value(1), date_value(2), None, date_value(3)])
    columns = [bp_col(), passthrough("p")]
    native, native_ev = _outcome(columns, [valued, later])
    forced, forced_ev = _outcome(
        [*columns, force_oracle(FORCE)], [with_force(valued), with_force(later)]
    )
    assert len(native_ev) == 1 and native_ev[0].native_admitted is True
    assert len(forced_ev) == 1 and forced_ev[0].native_admitted is False
    assert forced_reason(FORCE) in (forced_ev[0].reroute_reason or "")
    if isinstance(forced, tuple):
        assert native == forced
    else:
        assert not isinstance(native, tuple), native
        for got, want in zip(native, forced, strict=True):
            assert identical(got, want.drop_columns([FORCE]))


@NEEDS_COMPANION
@pytest.mark.parametrize("threads", [1, 4])
def test_a_multi_chunk_fifty_thousand_row_run_is_identical_on_both_legs(threads: int) -> None:
    values = [None if i % 11 == 3 else date_value(i) for i in range(100_003)]
    native, forced = run_pair(
        [bp_col(bucket="quarter"), passthrough("p")],
        split(source(values), 50_000),
        native_threads=threads,
    )
    assert len(native.out) == 3
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
def test_two_namespaces_give_independent_output_and_both_match_the_oracle() -> None:
    table = pa.table(
        {
            "a": pa.array([date_value(i) for i in range(12)], pa.string()),
            "b": pa.array([date_value(i) for i in range(12)], pa.string()),
        }
    )
    columns = [bp_col("a", namespace="ns_a"), bp_col("b", namespace="ns_b")]
    native, forced = run_pair(columns, split(table, 5))
    assert_same_as_oracle(native, forced)
    a, b = column_values(native.out, "a"), column_values(native.out, "b")
    assert a != b


# ---------------------------------------------------------------------------
# 2. Output type: content-dependent, exact, and equal on both legs.
# ---------------------------------------------------------------------------


def _types(run_out: list[pa.Table]) -> list[pa.DataType]:
    return [t.schema.field("d").type for t in run_out]


def _reassembled(run_out: list[pa.Table]) -> pa.DataType:
    chunks = [t.drop_columns([FORCE]) if FORCE in t.column_names else t for t in run_out]
    return concat_masked_chunks(chunks, table=TABLE).schema.field("d").type


_TYPE_CASES: dict[str, tuple[Callable[[], list[pa.Table]], list[pa.DataType], pa.DataType]] = {
    "zero_row": (lambda: [source([])], [pa.null()], pa.null()),
    "non_empty_all_null": (lambda: [source([None] * 5)], [pa.null()], pa.null()),
    "all_unparseable_non_null": (
        lambda: [source(["bad", "worse", "2021-13-45"])],
        [pa.string()],
        pa.string(),
    ),
    "valued": (lambda: [source([date_value(i) for i in range(5)])], [pa.string()], pa.string()),
    "null_then_valued": (
        lambda: [source([None] * 4), source([date_value(i) for i in range(4)])],
        [pa.null(), pa.string()],
        pa.string(),
    ),
    "valued_then_null": (
        lambda: [source([date_value(i) for i in range(4)]), source([None] * 4)],
        [pa.string(), pa.null()],
        pa.string(),
    ),
    "empty_then_valued": (
        lambda: [source([]), source([date_value(i) for i in range(4)])],
        [pa.null(), pa.string()],
        pa.string(),
    ),
    "all_empty": (lambda: [source([]), source([])], [pa.null(), pa.null()], pa.null()),
    "all_null_chunks": (
        lambda: [source([None] * 3), source([None] * 2)],
        [pa.null(), pa.null()],
        pa.null(),
    ),
}


@NEEDS_COMPANION
@pytest.mark.parametrize("case", sorted(_TYPE_CASES))
def test_output_type_is_exact_and_equal_on_both_legs(case: str) -> None:
    make_chunks, per_chunk, reassembled = _TYPE_CASES[case]
    native, forced = run_pair([bp_col(), passthrough("p")], make_chunks())
    assert native.ev[0].native_admitted is True
    assert _types(native.out) == per_chunk, "native per-chunk type"
    assert _types(forced.out) == per_chunk, "oracle per-chunk type"
    assert _reassembled(native.out) == reassembled
    assert _reassembled(forced.out) == reassembled
    for got, want in zip(native.out, forced.out, strict=True):
        assert got.schema.field("d").equals(want.schema.field("d"), check_metadata=True)


# ---------------------------------------------------------------------------
# 3. Determinism across chunks and against the full-frame route.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
def test_same_value_perturbs_the_same_in_every_chunk_and_matches_full_frame(
    bucket: str, tmp_path: Path
) -> None:
    # Leap February (2020, 2024) and non-leap February (2021) straddle chunk boundaries,
    # and the same value appears in different chunks.
    values: list[str | None] = [
        "2020-02-10",
        "2021-02-10",
        "2024-02-29",
        None,
        "2020-02-10",
        "2019-12-31",
        "2024-02-29",
        "2021-02-10",
        "2020-03-31",
        "2020-02-10",
    ]
    config = make_config([bp_col(bucket=bucket), passthrough("p")])
    table = source(values)
    run = run_one(config, split(table, 3))
    assert run.ev[0].native_admitted is True
    got = column_values(run.out, "d")
    seen: dict[str, str] = {}
    for src, out in zip(values, got, strict=True):
        if src is None:
            assert out is None
            continue
        assert seen.setdefault(src, out) == out
    full = _full_frame(config, table, tmp_path)
    assert got == full.column("d").to_pylist()


# ---------------------------------------------------------------------------
# 4. Leap-year grouping: one derive call per distinct bucket size per column-chunk.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
def test_derive_calls_are_one_per_distinct_bucket_size_per_column_chunk(
    bucket: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks_values: list[list[str | None]] = [
        ["2020-02-10", "2021-02-10", "2024-02-29", "2020-03-05"],
        [None, None],
        ["not-a-date", "bad"],
        [],
        ["2020-02-10", "2020-02-11"],
        ["2021-01-01", "2021-04-01", "2021-07-01", "2021-10-01", None, "2020-01-01"],
    ]
    spy = spy_index_kernel(monkeypatch)
    run = run_one(
        make_config([bp_col(bucket=bucket), passthrough("p")]),
        [source(v) for v in chunks_values],
    )
    assert run.ev[0].native_admitted is True
    expected = [expected_derive_sizes(v, bucket) for v in chunks_values]
    assert sorted(spy.pool_sizes()) == sorted(s for sizes in expected for s in sizes)
    # Degenerate chunks (null-only, unparseable-only, empty) contribute no derive call.
    assert expected[1] == expected[2] == expected[3] == []
    # kernel_calls counts branch executions, a different counter from derive calls.
    assert run.ev[0].kernel_calls["bucket_perturb"] == len(chunks_values)


# ---------------------------------------------------------------------------
# 5. Null and unparseable preservation across chunk boundaries.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("size", [1, 2, 3, 50])
def test_nulls_and_unparseable_values_pass_through_unchanged(size: int) -> None:
    values: list[str | None] = [
        date_value(1),
        None,
        "not-a-date",
        date_value(2),
        "2021-13-45",
        None,
        "",
        date_value(3),
    ]
    native, forced = run_pair([bp_col(), passthrough("p")], split(source(values), size))
    assert_same_as_oracle(native, forced)
    got = column_values(native.out, "d")
    for src, out in zip(values, got, strict=True):
        if src is None or src in ("not-a-date", "2021-13-45", ""):
            assert out == src
    parseable = (0, 3, 7)
    assert any(got[k] != values[k] for k in parseable)


@NEEDS_COMPANION
def test_an_all_unparseable_chunk_stays_string_typed() -> None:
    run = run_one(
        make_config([bp_col(), passthrough("p")]),
        [source(["junk", "more junk"]), source([date_value(1)])],
    )
    assert _types(run.out) == [pa.string(), pa.string()]
    assert column_values(run.out, "d")[:2] == ["junk", "more junk"]


# ---------------------------------------------------------------------------
# Thread-count invariance.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_output_bytes_do_not_depend_on_the_thread_count() -> None:
    values = [None if i % 9 == 4 else date_value(i) for i in range(6_000)]
    config = make_config([bp_col(bucket="month"), passthrough("p")])
    chunks = split(source(values), 1_700)
    first = run_one(config, chunks, native_threads=1)
    assert first.ev[0].native_admitted is True
    for threads in (2, 4, 8):
        other = run_one(config, chunks, native_threads=threads).out
        assert len(other) == len(first.out)
        assert all(identical(a, b) for a, b in zip(first.out, other, strict=True)), threads
