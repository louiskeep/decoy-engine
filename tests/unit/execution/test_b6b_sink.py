"""Acceptance tests 13 and 14 of plan 2026-10-02-b6b-lazy-batch-input (rev 2.1): the evidence
accumulator is flat in chunks, and the schema hold-back spills where the sink says.

These tests are written before the implementation. Do not delete one, add a skip or xfail
outside `NEEDS_COMPANION`, or relax a comparison without a new plan gate.
"""

from __future__ import annotations

import gc
import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6b_support as b6b
from tests.unit.execution.test_b6a_output import _LATE_NULL_CHUNKS, _late_typed_job

ROUTES = [pytest.param("native", marks=support.NEEDS_COMPANION), "oracle"]


# ---------------------------------------------------------------------------
# Test 13: the accumulator is flat in chunks.
# ---------------------------------------------------------------------------


def _accumulator() -> Any:
    return b6b.b6a.sink_module().OutputEvidenceAccumulator()


def _job(route: str, tmp_path: Path, rows: int) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(rows)]),
            "r": pa.array([f"s{i}" for i in range(rows)]),
        }
    )
    columns = [support.hash_col("h"), support.redact_col("r")]
    return support.make_cfg(columns, path=support.write_source(src, tmp_path / "s.parquet")), src


def _live_results() -> int:
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, ExecutionResult))


def _run_sampling(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: int, chunk: int
) -> tuple[tuple[int, ...], list[int]]:
    """Run a streamed job; return the accumulator's retained counts and the number of live
    `ExecutionResult` objects sampled at two points of the run."""
    if route == "oracle":
        support.remove_companion(monkeypatch)
    cfg, src = _job(route, tmp_path, rows)
    chunks = -(-rows // chunk)
    samples: list[int] = []
    holder: dict[str, b6b.b6a.ChunkSpy] = {}

    def before_pull(_table: str, index: int) -> None:
        if index in (chunks // 10, chunks - 2):
            samples.append(_live_results())

    holder["spy"] = b6b.b6a.ChunkSpy(monkeypatch, before_pull=before_pull)
    sink = b6b.CountingSink()
    b6b.run(cfg, {b6b.TABLE: src}, sink, chunk_size_rows=chunk, auto_chunk_threshold_rows=1)
    accumulator = holder["spy"].result_lists[b6b.TABLE]
    return accumulator.retained(), samples


@pytest.mark.parametrize("route", ROUTES)
def test_retained_evidence_does_not_grow_with_the_number_of_chunks(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    many_dir, few_dir = tmp_path / "many", tmp_path / "few"
    many_dir.mkdir()
    few_dir.mkdir()
    many, many_live = _run_sampling(route, many_dir, monkeypatch, 20_000, 10)
    few, few_live = _run_sampling(route, few_dir, monkeypatch, 2_000, 10)
    assert many == few
    assert max(many_live) <= min(many_live) + 3, many_live
    assert max(many_live) <= 8


def _chunk_results(route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The full per-chunk results of a resident run (B2's own list)."""
    if route == "oracle":
        support.remove_companion(monkeypatch)
    rows = 200
    cfg, src = _job(route, tmp_path, rows)
    spy = b6b.b6a.ChunkSpy(monkeypatch)
    b6b.run(
        cfg,
        {b6b.TABLE: src},
        None,
        chunk_size_rows=16,
        auto_chunk_threshold_rows=1,
        **b6b.b6a.resident_kw(),
    )
    return list(spy.result_lists[b6b.TABLE])


@pytest.mark.parametrize("route", ROUTES)
def test_the_accumulator_equals_b2s_list_aggregators(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution._chunked import aggregate_chunk_timings, aggregate_chunk_warnings
    from decoy_engine.execution._chunked_code_set import aggregate_chunk_code_set_corpora
    from decoy_engine.execution.native._chunked_evidence import aggregate_chunked_route_evidence

    results = _chunk_results(route, tmp_path, monkeypatch)
    assert len(results) == 13
    acc = _accumulator()
    for result in results:
        acc.append(result)
    assert acc.timings() == aggregate_chunk_timings(results)
    assert acc.warnings() == aggregate_chunk_warnings(results)
    assert acc.code_set_corpora() == aggregate_chunk_code_set_corpora(results)
    assert acc.route_evidence() == aggregate_chunked_route_evidence(results)
    assert acc.boundary_conversion_ms == sum(r.boundary_conversion_ms for r in results)
    assert acc.chunks == len(results)


def test_the_accumulator_keeps_no_execution_result_after_append() -> None:
    from decoy_engine.instrumentation.timing import StrategyTimingRecord

    acc = _accumulator()
    refs = []
    for index in range(50):
        result = ExecutionResult(
            outputs={"t": pa.table({"a": [index]})},
            timings=(
                StrategyTimingRecord(
                    strategy_type="redact", column="a", elapsed_ms=1.0, peak_memory_delta_kb=index
                ),
            ),
            boundary_conversion_ms=0.5,
        )
        import weakref

        refs.append(weakref.ref(result))
        acc.append(result)
        del result
    gc.collect()
    assert all(r() is None for r in refs)
    assert acc.retained()[0] == 1
    assert acc.timings()[0].peak_memory_delta_kb == 49
    assert acc.timings()[0].elapsed_ms == 50.0


def test_the_accumulator_dedups_equal_warnings_and_keeps_first_emission_order() -> None:
    from decoy_engine.generation.pool._events import QualityWarning

    acc = _accumulator()
    a = QualityWarning(code="a", provider="p", detail={"k": 1})
    b = QualityWarning(code="b", provider="p", detail={"k": 2})
    for warnings in ((a,), (a, b), (b, a), ()):
        acc.append(
            ExecutionResult(
                outputs={},
                warnings=warnings,
            )
        )
    assert acc.warnings() == (a, b)
    assert acc.retained()[1] == 2


# ---------------------------------------------------------------------------
# Test 14: the spill directory comes from the sink.
# ---------------------------------------------------------------------------


def _readonly_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    locked = tmp_path / "locked_tmp"
    locked.mkdir()
    locked.chmod(0o555)
    monkeypatch.setattr(tempfile, "tempdir", str(locked))
    monkeypatch.setenv("TMPDIR", str(locked))
    return locked


def _holds(spy_log: list[tuple[str | None, Any]]) -> list[Any]:
    return [d for p, d in spy_log if p == "_decoy_hold_"]


def _spy_mkdtemp(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, Any]]:
    log: list[tuple[str | None, Any]] = []
    real = tempfile.mkdtemp

    def spy(suffix: Any = None, prefix: Any = None, dir: Any = None) -> str:
        log.append((prefix, dir))
        return real(suffix, prefix, dir)

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    return log


def _hold_back_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    mod = b6b.b6a.sink_module()
    monkeypatch.setattr(mod, "ROW_GROUP_ROWS", 20)
    return _late_typed_job(tmp_path)


def test_a_nested_target_spills_under_its_own_spill_parent_not_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, src = _hold_back_fixture(tmp_path, monkeypatch)
    expected = b6b.b6a.reference(cfg, src)[support.TABLE]
    locked = _readonly_tmp(tmp_path, monkeypatch)
    log = _spy_mkdtemp(monkeypatch)
    target = tmp_path / "level_one" / "level_two" / "out"
    assert not target.parent.exists()
    inner = ParquetTransactionalSink(target)
    sink = b6b.b6a.RecordingSink(inner)
    result = b6b.b6a.run_streamed(cfg, src, sink)
    block = result.quality_metrics["auto_chunk"]["output"]
    assert block["held_back_chunks"] == _LATE_NULL_CHUNKS and block["spilled_chunks"] > 0
    assert inner.spill_parent == target.parent
    holds = _holds(log)
    assert holds and all(Path(d) == target.parent for d in holds), holds
    assert not any(p.name.startswith("_decoy_hold_") for p in target.parent.iterdir())
    assert [p.name for p in target.iterdir()] == [f"{support.TABLE}.parquet"]
    b6b.b6a.assert_streamed_equals(sink, expected)
    assert list(locked.iterdir()) == []


def test_an_injected_failure_leaves_no_hold_directory_under_the_spill_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, src = _hold_back_fixture(tmp_path, monkeypatch)
    _readonly_tmp(tmp_path, monkeypatch)
    b6b.b6a.ChunkSpy(monkeypatch, fail_at=_LATE_NULL_CHUNKS + 1)
    target = tmp_path / "deep" / "er" / "out"
    sink = b6b.b6a.RecordingSink(ParquetTransactionalSink(target))
    with pytest.raises(RuntimeError, match="injected kernel failure"):
        b6b.b6a.run_streamed(cfg, src, sink)
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    assert not target.exists()
    assert b6b.b6a.leftovers(target.parent) == []


def test_a_sink_without_a_spill_parent_fails_clearly_when_the_hold_back_must_spill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, src = _hold_back_fixture(tmp_path, monkeypatch)
    log = _spy_mkdtemp(monkeypatch)
    sink = b6b.b6a.RecordingSink()
    assert not hasattr(sink, "spill_parent")
    with pytest.raises(ExecutionError) as err:
        b6b.b6a.run_streamed(cfg, src, sink)
    assert err.value.code == "hold_back_spill_unavailable"
    assert support.TABLE in str(err.value)
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    assert _holds(log) == []


def test_a_sink_without_a_spill_parent_streams_normally_when_nothing_spills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, src = _late_typed_job(tmp_path)
    expected = b6b.b6a.reference(cfg, src)[support.TABLE]
    log = _spy_mkdtemp(monkeypatch)
    sink = b6b.b6a.RecordingSink()
    result = b6b.b6a.run_streamed(cfg, src, sink)
    assert result.quality_metrics["auto_chunk"]["output"]["spilled_chunks"] == 0
    assert result.quality_metrics["auto_chunk"]["output"]["held_back_chunks"] == _LATE_NULL_CHUNKS
    assert sink.count("commit") == 1
    assert _holds(log) == []
    b6b.b6a.assert_streamed_equals(sink, expected)


def test_spill_parent_is_the_parent_of_the_staging_target(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(tmp_path / "a" / "out")
    assert sink.spill_parent == tmp_path / "a"
    assert not (tmp_path / "a").exists()
