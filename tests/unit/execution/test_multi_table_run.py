"""B7 acceptance tests 6 to 10: evidence, order and errors, threads, memory, A8 and lazy sources.

Plan: docs/plans/2026-10-01-multi-table-dispatch.md (revision 3), guarantees 4 to 8 and
Design 4 to 8.
"""

from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import ExecutionError, run_pipeline
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _multi_table_support as mt

pytestmark = pytest.mark.filterwarnings("ignore")

N = mt.BIG


def _redact_only(tag: str, n: int, column: str = "r") -> mt.TableSpec:
    """A table the native route runs without the compiled companion."""
    return (
        [support.redact_col(column)],
        pa.table({column: pa.array([f"{tag}{i}" for i in range(n)])}),
    )


def _spy_auto_chunk(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Entry and exit order of every `run_auto_chunk` call, with its arguments."""
    from decoy_engine.execution import _pipeline_auto_chunk as ac

    events: list[dict[str, Any]] = []
    real = ac.run_auto_chunk

    def spy(config: Any, source: Any, **kwargs: Any) -> Any:
        events.append({"event": "enter", "table": kwargs["table"], "kwargs": kwargs})
        try:
            return real(config, source, **kwargs)
        finally:
            events.append({"event": "exit", "table": kwargs["table"]})

    monkeypatch.setattr(ac, "run_auto_chunk", spy)
    return events


def _entered(events: list[dict[str, Any]]) -> list[str]:
    return [e["table"] for e in events if e["event"] == "enter"]


def _never_overlapping(events: list[dict[str, Any]]) -> bool:
    depth = 0
    for event in events:
        depth += 1 if event["event"] == "enter" else -1
        if depth > 1:
            return False
    return depth == 0


# ---------------------------------------------------------------------------
# Test 6: evidence.
# ---------------------------------------------------------------------------

_LANE_KEYS = {"lane", "lane_reason", "native_threads"}
_REPRO_KEYS = {"mode", "chunk_size_rows", "threshold_rows", "source_rows", "chunk_count", "reason"}


def _evidence_job(tmp_path: Path, rows: int = N) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    return mt.build_job(
        tmp_path,
        {
            "z": _redact_only("z", rows),
            "small": _redact_only("s", mt.SMALL),
            "a": _redact_only("a", rows + 5),
        },
    )


def _check_evidence(result: Any, *, chunk: int, threshold: int, threads: int, rows: int) -> None:
    block = result.quality_metrics["auto_chunk"]
    assert set(block) == _REPRO_KEYS | _LANE_KEYS | {"tables"}
    assert block["mode"] == "chunked"
    assert block["chunk_size_rows"] == chunk and block["threshold_rows"] == threshold
    assert block["source_rows"] is None and block["chunk_count"] is None
    assert block["reason"] == "multi_table_split: 2 of 3 mask tables dispatched (z, a)"
    assert block["lane"] == "dispatcher" and block["lane_reason"] is None
    assert block["native_threads"] == threads
    assert [t["table"] for t in block["tables"]] == ["z", "small", "a"]
    by_name = {t["table"]: t for t in block["tables"]}
    for name, size in (("z", rows), ("a", rows + 5)):
        entry = by_name[name]
        assert set(entry) == {
            "table",
            "dispatched",
            "source_rows",
            "chunk_count",
            "reason",
            "lane",
            "lane_reason",
            "native_threads",
            "output",
            "input",
        }
        assert entry["dispatched"] is True and entry["source_rows"] == size
        assert entry["chunk_count"] == -(-size // chunk)
        assert entry["lane"] == "dispatcher" and entry["lane_reason"] is None
        assert entry["native_threads"] == threads
    small = by_name["small"]
    assert set(small) == {"table", "dispatched", "source_rows", "chunk_count", "reason"}
    assert small["dispatched"] is False and small["chunk_count"] is None
    assert small["source_rows"] == mt.SMALL
    assert "threshold" in small["reason"]
    assert "chunked_route" not in result.quality_metrics
    per_table = result.quality_metrics["chunked_route_by_table"]
    assert set(per_table) == {"z", "a"}
    for payload in per_table.values():
        assert set(payload) >= {"native_admitted", "reroute_reason", "pandas_read_passthrough"}
        for col in payload["columns"]:
            assert "elapsed_ms" not in col
            assert set(col) >= {
                "column",
                "strategy",
                "planned_backend",
                "executed_backend",
                "calls",
            }


def test_evidence_under_non_default_auto_chunk_knobs(tmp_path: Path) -> None:
    cfg, sources = _evidence_job(tmp_path)
    got = run_pipeline(cfg, sources=sources, **mt.kw(native_threads=2))
    _check_evidence(got, chunk=mt.CHUNK, threshold=mt.THRESHOLD, threads=2, rows=N)


def test_evidence_under_default_auto_chunk_knobs(tmp_path: Path) -> None:
    """Both stamp paths of `stamp_execution_metrics`: with every auto-chunk knob at its
    default, a split call still stamps the block (a plain full-frame call stamps nothing)."""
    rows = 100_000
    cfg, sources = _evidence_job(tmp_path, rows)
    got = run_pipeline(cfg, sources=sources, engine_version=mt.ENGINE_VERSION)
    _check_evidence(got, chunk=50_000, threshold=100_000, threads=1, rows=rows)


def test_evidence_is_json_safe_deterministic_and_round_trips(tmp_path: Path) -> None:
    cfg, sources = _evidence_job(tmp_path)
    one = run_pipeline(cfg, sources=sources, **mt.kw())
    two = run_pipeline(cfg, sources=sources, **mt.kw())
    text = json.dumps(one.quality_metrics, allow_nan=False, sort_keys=True)
    assert json.loads(text) == json.loads(json.dumps(two.quality_metrics, sort_keys=True))
    assert one.quality_metrics == two.quality_metrics


def test_split_off_run_carries_no_split_evidence(tmp_path: Path) -> None:
    cfg, sources = _evidence_job(tmp_path)
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert "chunked_route_by_table" not in off.quality_metrics
    assert "tables" not in off.quality_metrics.get("auto_chunk", {})
    on = run_pipeline(cfg, sources=sources, **mt.kw())
    assert "chunked_route_by_table" in on.quality_metrics


def test_companion_absent_run_reports_the_hash_column_rerouted_to_the_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support.remove_companion(monkeypatch)
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "big": (mt.std_columns("big_ns"), mt.string_table(N, "b")),
            "small": (mt.std_columns("small_ns"), mt.string_table(mt.SMALL, "s")),
        },
    )
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    route = mt.route_of(got, "big")
    assert route is not None
    assert route["native_admitted"] is False
    assert route["reroute_reason"].startswith("crypto_extension_unavailable")
    hash_col = next(c for c in route["columns"] if c["column"] == "h")
    assert hash_col["planned_backend"] == "rust_companion"
    assert hash_col["executed_backend"] == "pandas_oracle"


# ---------------------------------------------------------------------------
# Test 7: order and errors.
# ---------------------------------------------------------------------------


def test_dispatched_tables_run_in_config_order_then_the_group_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "z": _redact_only("z", N),
            "tiny": _redact_only("t", mt.SMALL),
            "m": _redact_only("m", N + 3),
            "a": _redact_only("a", N + 6),
        },
    )
    reordered = {name: sources[name] for name in ("a", "tiny", "m", "z")}
    events = _spy_auto_chunk(monkeypatch)
    adapter_calls = mt.spy_adapter_run(monkeypatch)
    run_pipeline(cfg, sources=reordered, **mt.kw())
    assert _entered(events) == ["z", "m", "a"]
    assert _never_overlapping(events)
    # The group runs once, after the last dispatched table has finished.
    assert len(adapter_calls) >= 1
    assert events[-1] == {"event": "exit", "table": "a"}


def test_a_kernel_failure_on_the_second_dispatched_table_stops_everything_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.native import _chunked_entry

    cfg, sources = mt.build_job(
        tmp_path,
        {
            "t1": _redact_only("1", N),
            "t2": _redact_only("2", N),
            "t3": _redact_only("3", N),
            "tiny": _redact_only("t", mt.SMALL),
        },
    )
    events = _spy_auto_chunk(monkeypatch)
    adapter_calls = mt.spy_adapter_run(monkeypatch)
    real = _chunked_entry._mask_chunk_native

    def failing(*args: Any, **kwargs: Any) -> Any:
        current = [e["table"] for e in events if e["event"] == "enter"][-1]
        if current == "t2":
            raise ExecutionError(code="test_kernel_failure", message="injected on t2")
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunked_entry, "_mask_chunk_native", failing)
    with pytest.raises(ExecutionError) as raised:
        run_pipeline(cfg, sources=sources, **mt.kw())
    assert raised.value.code == "test_kernel_failure"
    assert _entered(events) == ["t1", "t2"]
    assert adapter_calls == []


def _failing_text_mask(monkeypatch: pytest.MonkeyPatch, columns: set[str]) -> None:
    """Make the `text_mask` handler fail for the named columns on every route. text_mask has
    no native operator, so every route reaches this handler."""
    from decoy_engine.execution._strategies import SCALAR_HANDLERS

    handler = SCALAR_HANDLERS["text_mask"]
    real = handler.run

    def run(df: Any, column: str, plan: Any, ctx: Any) -> Any:
        if column in columns:
            raise ExecutionError(code=f"boom_{column}", message=f"injected on {column}")
        return real(df, column, plan, ctx)

    monkeypatch.setattr(handler, "run", run)


def _text_table(column: str, n: int) -> mt.TableSpec:
    return (
        [{"name": column, "strategy": "text_mask", "namespace": f"tm_{column}"}],
        pa.table({column: pa.array([f"call 555-01{i % 100:02d} today" for i in range(n)])}),
    )


def test_first_reported_failure_among_independent_tables_follows_config_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = mt.build_job(
        tmp_path, {"z": _text_table("zcol", N), "a": _text_table("acol", N)}
    )
    _failing_text_mask(monkeypatch, {"zcol", "acol"})
    with pytest.raises(ExecutionError) as split:
        run_pipeline(cfg, sources=sources, **mt.kw())
    with pytest.raises(ExecutionError) as off:
        run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert split.value.code == "boom_zcol"
    assert off.value.code == "boom_acol"


def test_a_failing_dispatched_table_wins_over_a_failing_group_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = mt.build_job(
        tmp_path, {"z": _text_table("zcol", N), "a": _text_table("acol", mt.SMALL)}
    )
    _failing_text_mask(monkeypatch, {"zcol", "acol"})
    adapter_calls = mt.spy_adapter_run(monkeypatch)
    with pytest.raises(ExecutionError) as split:
        run_pipeline(cfg, sources=sources, **mt.kw())
    assert split.value.code == "boom_zcol"
    # B1's oracle route also calls the adapter, per chunk, for the dispatched table; only a
    # call that carries the group table "a" would be the group starting.
    group_calls = [c for c in adapter_calls if "a" in c[0][1]]
    assert group_calls == [], "the group must not start after a dispatched failure"


def test_row_error_aggregation_differs_as_declared(tmp_path: Path) -> None:
    def dated(tag: str, n: int) -> mt.TableSpec:
        values = ["2020-01-01"] * (n - 1) + ["not-a-date"]
        return (
            [
                {
                    "name": "d",
                    "strategy": "date_shift",
                    "namespace": f"{tag}_ns",
                    "provider_config": {
                        "min_days": -5,
                        "max_days": 5,
                        "date_format": "%Y-%m-%d",
                    },
                }
            ],
            pa.table({"d": pa.array(values)}),
        )

    cfg, sources = mt.build_job(tmp_path, {"big": dated("big", N), "small": dated("small", 6)})
    with pytest.raises(RowErrorsFailedError) as split:
        run_pipeline(cfg, sources=sources, **mt.kw())
    with pytest.raises(RowErrorsFailedError) as off:
        run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert {r.table for r in split.value.records} == {"big"}
    assert {r.table for r in off.value.records} == {"big", "small"}
    chunk_rows = N - (N - 1) // mt.CHUNK * mt.CHUNK
    assert len(split.value.records) == 1 and chunk_rows >= 1


# ---------------------------------------------------------------------------
# Test 8: threads.
# ---------------------------------------------------------------------------


def test_every_dispatched_table_receives_the_job_thread_budget_and_never_overlaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.native import _kernels_keyed

    if not support.COMPANION_PRESENT:
        pytest.skip("compiled companion not installed")
    real = _kernels_keyed.load_compiled_crypto_kernel()
    seen: list[Any] = []

    class _Recording:
        def derive_batch(self, values: Any, *, native_threads: Any = None, **kw: Any) -> Any:
            seen.append(native_threads)
            return real.derive_batch(values, native_threads=native_threads, **kw)

    monkeypatch.setattr(_kernels_keyed, "load_compiled_crypto_kernel", lambda: _Recording())
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "a": (mt.std_columns("a_ns"), mt.string_table(N, "a")),
            "b": (mt.std_columns("b_ns"), mt.string_table(N + 2, "b")),
            "tiny": (mt.std_columns("t_ns"), mt.string_table(mt.SMALL, "t")),
        },
    )
    events = _spy_auto_chunk(monkeypatch)
    one = run_pipeline(cfg, sources=sources, **mt.kw(native_threads=1))
    assert set(seen) == {1}
    seen.clear()
    del events[:]
    four = run_pipeline(cfg, sources=sources, **mt.kw(native_threads=4))
    assert set(seen) == {4}
    assert [e["kwargs"]["native_threads"] for e in events if e["event"] == "enter"] == [4, 4]
    assert _never_overlapping(events)
    for name in one.outputs:
        assert one.outputs[name].equals(four.outputs[name], check_metadata=True)


def test_a_call_with_no_dispatched_table_ignores_the_thread_budget(tmp_path: Path) -> None:
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "a": _redact_only("a", mt.SMALL),
            "b": _redact_only("b", mt.SMALL),
        },
    )
    one = run_pipeline(cfg, sources=sources, **mt.kw(native_threads=1))
    four = run_pipeline(cfg, sources=sources, **mt.kw(native_threads=4))
    for name in one.outputs:
        assert one.outputs[name].equals(four.outputs[name], check_metadata=True)


# ---------------------------------------------------------------------------
# Test 9: memory structure.
# ---------------------------------------------------------------------------


def test_chunk_lists_are_dead_before_the_next_dispatched_table_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline_auto_chunk as ac

    cfg, sources = mt.build_job(
        tmp_path,
        {"a": _redact_only("a", N), "b": _redact_only("b", N), "c": _redact_only("c", N)},
    )
    probes: dict[str, list[weakref.ref[Any]]] = {}
    real_join = ac.join_dispatcher_chunks
    current: list[str] = []

    def join(chunks: list[pa.Table], *, table: str) -> pa.Table:
        probes[table] = [weakref.ref(chunk) for chunk in chunks]
        return real_join(chunks, table=table)

    monkeypatch.setattr(ac, "join_dispatcher_chunks", join)
    real_run = ac.run_auto_chunk
    alive_at_start: dict[str, int] = {}

    def spy(config: Any, source: Any, **kwargs: Any) -> Any:
        gc.collect()
        alive_at_start[kwargs["table"]] = sum(
            ref() is not None for refs in probes.values() for ref in refs
        )
        current.append(kwargs["table"])
        return real_run(config, source, **kwargs)

    monkeypatch.setattr(ac, "run_auto_chunk", spy)
    run_pipeline(cfg, sources=sources, **mt.kw())
    assert current == ["a", "b", "c"]
    assert alive_at_start == {"a": 0, "b": 0, "c": 0}
    assert len(probes) == 3 and all(len(refs) == 3 for refs in probes.values())


def test_calls_to_the_dispatcher_lane_never_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = mt.build_job(tmp_path, {"a": _redact_only("a", N), "b": _redact_only("b", N)})
    events = _spy_auto_chunk(monkeypatch)
    run_pipeline(cfg, sources=sources, **mt.kw())
    assert _entered(events) == ["a", "b"]
    assert _never_overlapping(events)


# ---------------------------------------------------------------------------
# Test 10: A8 and lazy sources.
# ---------------------------------------------------------------------------


def test_a_transform_bearing_table_stays_in_the_group_and_its_transforms_apply_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline

    cfg, sources = mt.build_job(
        tmp_path,
        {"anchor": _redact_only("a", N), "tx": _redact_only("x", N + 10)},
    )
    for table in cfg["tables"]:
        if table["name"] == "tx":
            table["transforms"] = [{"op": "limit", "n": 25}]
    prepared: list[int] = []
    real = _pipeline.prepare_transform_sources

    def spy(*args: Any, **kwargs: Any) -> Any:
        prepared.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(_pipeline, "prepare_transform_sources", spy)
    split_calls = mt.spy_split(monkeypatch)
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    assert prepared == [1]
    if mt.split_supported():
        assert len(split_calls) == 1
    entries = {t["table"]: t for t in got.quality_metrics["auto_chunk"]["tables"]}
    assert entries["anchor"]["dispatched"] is True
    assert entries["tx"]["dispatched"] is False
    assert "per_table_transforms_present" in entries["tx"]["reason"]
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert got.outputs["tx"].num_rows == 25
    assert got.outputs["tx"].equals(off.outputs["tx"], check_metadata=True)


def test_a_lazy_source_dispatches_like_its_twin_and_is_never_read_by_the_split_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyarrow.parquet as pq

    from decoy_engine.profile._readers import LazySource

    cfg, sources = mt.build_job(
        tmp_path,
        {"anchor": _redact_only("a", N), "lazy": _redact_only("l", N)},
    )
    lazy_path = Path(cfg["sources"]["lazy"]["path"])
    handed: dict[str, Any] = {"anchor": sources["anchor"], "lazy": LazySource(lazy_path)}
    twin = run_pipeline(
        cfg, sources={"anchor": sources["anchor"], "lazy": pq.read_table(lazy_path)}, **mt.kw()
    )
    reads: list[str] = []

    def poison(name: str) -> Any:
        real = getattr(LazySource, name)

        def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
            import sys

            frame: Any = sys._getframe(1)
            while frame is not None:
                if frame.f_code.co_filename.endswith("_pipeline_multi_table.py"):
                    reads.append(name)
                frame = frame.f_back
            return real(self, *args, **kwargs)

        return spy

    for name in ("iter_batches", "open_batches"):
        monkeypatch.setattr(LazySource, name, poison(name))
    got = run_pipeline(cfg, sources=handed, **mt.kw())
    entries = {t["table"]: t for t in got.quality_metrics["auto_chunk"]["tables"]}
    twin_entries = {t["table"]: t for t in twin.quality_metrics["auto_chunk"]["tables"]}
    assert entries["anchor"]["dispatched"] is True
    assert entries["lazy"]["dispatched"] is True
    assert entries["lazy"]["reason"] == twin_entries["lazy"]["reason"]
    for name in twin.outputs:
        assert got.outputs[name].equals(twin.outputs[name], check_metadata=True), name
    assert reads == []


def test_the_split_logs_the_dispatched_tables_and_the_group_without_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    cfg, sources = mt.build_job(
        tmp_path,
        {
            "z": _redact_only("zsecret", N),
            "tiny": _redact_only("tinysecret", mt.SMALL),
            "a": _redact_only("asecret", N),
        },
    )
    with caplog.at_level(logging.INFO, logger="decoy_engine.execution._pipeline_multi_table"):
        run_pipeline(cfg, sources=sources, **mt.kw())
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "decoy_engine.execution._pipeline_multi_table"
    ]
    assert lines == ["multi-table split dispatched=z, a full_frame=tiny"]
    assert not any("secret" in r.getMessage() for r in caplog.records)
