"""F29 acceptance A7: counts follow consumption.

Controlled clocks and generated Arrow batches. Rows and Arrow logical bytes are accounted per
table and per channel scope, from buffer metadata alone, and only for batches the consumer
actually pulled: empty input, an early close, a failure after a batch, nested runs and several
tables can neither double-count nor imply completion.
"""

from __future__ import annotations

import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine import progress


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(progress.time, "perf_counter", fake)
    return fake


def _batch(n: int, start: int = 0) -> pa.Table:
    return pa.table(
        {
            "a": pa.array(range(start, start + n), pa.int64()),
            "b": pa.array([f"v{i}" for i in range(n)]),
        }
    )


def _rates(events: list[progress.ProgressEvent]) -> list[progress.RateSample]:
    return [e for e in events if isinstance(e, progress.RateSample)]


def test_rows_and_logical_bytes_are_exact_per_table(clock: _Clock) -> None:
    seen: list[progress.ProgressEvent] = []
    a, b = [_batch(3), _batch(5, 3)], [_batch(7)]
    with progress.use_progress(progress.CallbackSink(seen.append), interval_s=3600) as ch:
        assert list(ch.count_rows("a", a)) == a
        assert list(ch.count_rows("b", b)) == b
    finals = {e.table: e for e in _rates(seen) if e.final}
    assert finals["a"].rows_done == 8 and finals["a"].bytes_done == sum(t.nbytes for t in a)
    assert finals["b"].rows_done == 7 and finals["b"].bytes_done == sum(t.nbytes for t in b)
    assert {e.byte_basis for e in _rates(seen)} == {"arrow_logical"}  # one basis, never mixed


def test_counting_reads_metadata_only_and_keeps_the_iterator_lazy(clock: _Clock) -> None:
    pulled: list[int] = []

    def source():
        for i in range(5):
            pulled.append(i)
            yield _batch(2, i)

    with progress.use_progress(progress.CallbackSink(lambda e: None), interval_s=3600) as ch:
        it = ch.count_rows("t", source())
        assert pulled == []  # nothing is read until the consumer pulls
        next(it)
        assert pulled == [0]
        it.close()


def test_sample_count_is_bounded_by_the_interval(clock: _Clock) -> None:
    seen: list[progress.ProgressEvent] = []
    with progress.use_progress(progress.CallbackSink(seen.append), interval_s=10.0) as ch:
        for _ in ch.count_rows("t", (clock_step(clock, _batch(1)) for _ in range(100))):
            pass
    samples = _rates(seen)
    assert len(samples) <= 100 // 10 + 1
    assert sum(1 for s in samples if s.final) == 1
    assert samples[-1].final and samples[-1].rows_done == 100


def clock_step(clock: _Clock, chunk: pa.Table) -> pa.Table:
    clock.now += 1.0
    return chunk


def test_empty_input_emits_no_sample_and_implies_nothing(clock: _Clock) -> None:
    seen: list[progress.ProgressEvent] = []
    with progress.use_progress(progress.CallbackSink(seen.append)) as ch:
        assert list(ch.count_rows("t", [])) == []
    assert _rates(seen) == []


def test_early_close_counts_only_consumed_batches_and_flushes_once(clock: _Clock) -> None:
    seen: list[progress.ProgressEvent] = []
    batches = [_batch(4), _batch(4), _batch(4)]
    with progress.use_progress(progress.CallbackSink(seen.append), interval_s=3600) as ch:
        it = ch.count_rows("t", iter(batches))
        next(it)
        next(it)
        it.close()
        it.close()  # closing twice cannot emit a second final
    finals = [s for s in _rates(seen) if s.final]
    assert len(finals) == 1
    assert finals[0].rows_done == 8 and finals[0].bytes_done == batches[0].nbytes * 2


def test_failure_after_a_batch_is_not_counted_as_completion(clock: _Clock) -> None:
    seen: list[progress.ProgressEvent] = []
    with progress.use_progress(progress.CallbackSink(seen.append), interval_s=3600) as ch:
        it = ch.count_rows("t", iter([_batch(3), _batch(3)]))
        next(it)
        with pytest.raises(RuntimeError):
            it.throw(RuntimeError("downstream failed"))
    finals = [s for s in _rates(seen) if s.final]
    assert len(finals) == 1 and finals[0].rows_done == 3


def test_nested_channels_do_not_double_count(clock: _Clock) -> None:
    outer_seen: list[progress.ProgressEvent] = []
    inner_seen: list[progress.ProgressEvent] = []
    with progress.use_progress(progress.CallbackSink(outer_seen.append), interval_s=3600) as outer:
        list(outer.count_rows("t", [_batch(2)]))
        with progress.use_progress(
            progress.CallbackSink(inner_seen.append), interval_s=3600
        ) as inner:
            list(inner.count_rows("t", [_batch(5)]))
        with progress.use_progress(None) as same:
            assert same is outer
            list(same.count_rows("t", [_batch(1)]))
    assert [s.rows_done for s in _rates(outer_seen) if s.final] == [2, 1]
    assert [s.rows_done for s in _rates(inner_seen) if s.final] == [5]


def test_stored_bytes_are_a_separate_basis(clock: _Clock) -> None:
    seen: list[progress.ProgressEvent] = []
    with progress.use_progress(progress.CallbackSink(seen.append), interval_s=3600) as ch:
        list(ch.count_rows("t", [_batch(3)]))
        started = ch.phase_start("publish", table="t")
        ch.phase_finish("publish", started, table="t", rows_out=3, stored_bytes=1234)
    publish = [
        e
        for e in seen
        if isinstance(e, progress.PhaseEvent) and e.name == "publish" and e.status == "finish"
    ]
    assert publish[0].bytes_done == 1234 and publish[0].byte_basis == "stored_output"
    assert all(s.byte_basis == "arrow_logical" for s in _rates(seen))


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.integers(min_value=0, max_value=40), min_size=0, max_size=12),
    st.integers(min_value=0, max_value=12),
)
def test_counters_equal_the_consumed_prefix_for_any_partition(sizes: list[int], take: int) -> None:
    batches = [_batch(n) for n in sizes]
    seen: list[progress.ProgressEvent] = []
    with progress.use_progress(progress.CallbackSink(seen.append), interval_s=3600) as ch:
        it = ch.count_rows("t", iter(batches))
        consumed = 0
        for _ in range(min(take, len(batches))):
            next(it)
            consumed += 1
        it.close()
    finals = [s for s in _rates(seen) if s.final]
    expected_rows = sum(sizes[:consumed])
    if consumed == 0:
        assert finals == []
        return
    assert len(finals) == 1
    assert finals[0].rows_done == expected_rows
    assert finals[0].bytes_done == sum(t.nbytes for t in batches[:consumed])
    assert all(s.rows_done <= expected_rows for s in _rates(seen))
