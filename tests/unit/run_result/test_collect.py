"""AT-K1 to AT-K4 and AT-K7: the progress collector, from synthetic events and from real runs."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.chunked import observe_chunked
from decoy_engine.progress import PhaseEvent, RateSample, RouteEvent, use_progress
from decoy_engine.run_result import CollectedFacts, RunResultCollector, project_tables
from tests._declared_schema import chunked_entry_kwargs

_VERSION = "collect-test"


def _job(
    tmp_path: Path,
    names: tuple[str, ...],
    rows: int = 3,
    generate: str | None = None,
    fk: bool = False,
):
    sources: dict[str, Any] = {}
    in_memory: dict[str, pa.Table] = {}
    tables: list[dict] = []
    targets: dict[str, dict] = {}
    for name in names:
        df = pd.DataFrame({"id": [f"{name}-{i}" for i in range(rows)]})
        path = tmp_path / f"{name}.csv"
        df.to_csv(path, index=False)
        sources[name] = {"type": "file", "format": "csv", "has_header": True, "path": str(path)}
        in_memory[name] = pa.Table.from_pandas(df, preserve_index=False)
        tables.append(
            {"name": name, "columns": [{"name": "id", "strategy": "hash", "namespace": "k"}]}
        )
        targets[name] = {"type": "file", "format": "csv", "path": str(tmp_path / f"{name}_o.csv")}
    if generate:
        tables.append(
            {
                "name": generate,
                "row_count": 5,
                "generate_columns": [{"name": "seq", "type": "sequence", "start": 1, "step": 1}],
            }
        )
        targets[generate] = {
            "type": "file",
            "format": "csv",
            "path": str(tmp_path / f"{generate}_o.csv"),
        }
    # Sequential and out-of-core routes only accept a job that has relationships: chain the tables.
    relationships = [
        {
            "parent": {"table": parent, "columns": ["id"]},
            "children": [{"table": child, "columns": ["id"]}],
            "orphan_policy": "preserve",
            "namespace": "k",
        }
        for parent, child in pairwise(names)
    ]
    cfg = PipelineConfig.model_validate(
        {
            "version": 2,
            **({"relationships": relationships} if fk else {}),
            "global_settings": {"seed": 42},
            "sources": sources,
            "tables": tables,
            "targets": targets,
        }
    ).model_dump()
    return cfg, in_memory


def _project(cfg: dict, collector: RunResultCollector, *, run_failed: bool = False):
    result = project_tables(
        config=cfg,
        execution_result=None,
        facts=collector.snapshot(),
        delivery=None,
        run_failed=run_failed,
    )
    return {t.name: t for t in result.tables}


def _phase(name: str, status: str, table: str | None = None, **kw: Any) -> PhaseEvent:
    return PhaseEvent(name, status, table=table, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("route", "kwargs", "source"),
    [
        ("sequential", {"execution_mode": "sequential"}, "phase"),
        ("out_of_core", {"execution_mode": "out_of_core"}, "phase"),
    ],
)
def test_whole_table_routes_count_rows(tmp_path: Path, route: str, kwargs: dict, source: str):
    cfg, sources = _job(tmp_path, ("t1", "t2"), fk=True)
    collector = RunResultCollector()
    result = run_pipeline(
        cfg, sources=sources, engine_version=_VERSION, progress=collector, **kwargs
    )
    tables = _project(cfg, collector)
    for name in ("t1", "t2"):
        assert tables[name].rows_out == result.outputs[name].num_rows == 3
        assert tables[name].rows_source == source
        assert tables[name].outcome == "succeeded"


def test_generate_table_counts_rows_from_its_phase(tmp_path: Path):
    cfg, sources = _job(tmp_path, ("t1",), generate="g1")
    collector = RunResultCollector()
    run_pipeline(cfg, sources=sources, engine_version=_VERSION, progress=collector)
    row = _project(cfg, collector)["g1"]
    assert (row.rows_out, row.rows_source, row.outcome) == (5, "phase", "succeeded")


def test_full_frame_resident_mask_tables_have_no_rows(tmp_path: Path):
    cfg, sources = _job(tmp_path, ("t1", "t2"))
    collector = RunResultCollector()
    run_pipeline(cfg, sources=sources, engine_version=_VERSION, progress=collector)
    for name in ("t1", "t2"):
        row = _project(cfg, collector)[name]
        assert (row.rows_out, row.rows_source) == (None, None)


def test_per_table_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from decoy_engine.execution import _sequential

    real = _sequential.run_chunk_ingest_guards

    def boom(plan: Any, tables: dict, *rest: Any) -> Any:
        if "t2" in tables:
            raise RuntimeError("boom")
        return real(plan, tables, *rest)

    monkeypatch.setattr(_sequential, "run_chunk_ingest_guards", boom)
    cfg, sources = _job(tmp_path, ("t1", "t2", "t3"), fk=True)
    collector = RunResultCollector()
    with pytest.raises(RuntimeError):
        run_pipeline(
            cfg,
            sources=sources,
            engine_version=_VERSION,
            progress=collector,
            execution_mode="sequential",
        )
    tables = _project(cfg, collector, run_failed=True)
    assert [tables[n].outcome for n in ("t1", "t2", "t3")] == ["succeeded", "failed", "not_run"]


def test_full_frame_mask_failure_fails_every_mask_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

    def boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(PandasExecutionAdapter, "run", boom)
    cfg, sources = _job(tmp_path, ("t1", "t2"))
    collector = RunResultCollector()
    with pytest.raises(RuntimeError):
        run_pipeline(cfg, sources=sources, engine_version=_VERSION, progress=collector)
    tables = _project(cfg, collector, run_failed=True)
    assert [tables[n].outcome for n in ("t1", "t2")] == ["failed", "failed"]


def test_collector_never_changes_a_run(tmp_path: Path):
    cfg, sources = _job(tmp_path, ("t1",))
    plain = run_pipeline(cfg, sources=sources, engine_version=_VERSION)
    collector = RunResultCollector()
    observed = run_pipeline(cfg, sources=sources, engine_version=_VERSION, progress=collector)
    assert observed.outputs["t1"].equals(plain.outputs["t1"])


def test_on_event_swallows_bad_input():
    collector = RunResultCollector()
    before = collector.snapshot()

    class Exploding(PhaseEvent):
        @property
        def table(self) -> str:
            raise RuntimeError("read")

    exploding = PhaseEvent.__new__(Exploding)

    forged = _phase("mask", "start", "t1")
    object.__setattr__(forged, "table", "/etc/passwd")
    rate = RateSample("t1", 4, 1.0, 1.0, final=True)
    object.__setattr__(rate, "table", "https://x.test/p")
    for bad in (object(), None, "text", exploding, forged, rate):
        collector.on_event(bad)  # type: ignore[arg-type]
    assert collector.snapshot() == before


def test_phase_timings():
    collector = RunResultCollector()
    collector.on_event(_phase("resolve", "finish", elapsed_s=0.5))
    collector.on_event(_phase("mask", "finish", "t1", rows_out=1, elapsed_s=1.0))
    collector.on_event(_phase("mask", "finish", "t2", rows_out=1, elapsed_s=2.0))
    collector.on_event(_phase("mask", "error", "t3", elapsed_s=0.25))
    facts = collector.snapshot()
    assert facts.phases == (("resolve", 0.5), ("mask", 3.25))
    assert all(seconds >= 0 for _, seconds in facts.phases)
    assert facts.elapsed == {"t1": 1.0, "t2": 2.0, "t3": 0.25}


def test_phase_timings_on_a_real_run(tmp_path: Path):
    cfg, sources = _job(tmp_path, ("t1", "t2"), fk=True)
    collector = RunResultCollector()
    run_pipeline(
        cfg,
        sources=sources,
        engine_version=_VERSION,
        progress=collector,
        execution_mode="sequential",
    )
    names = [name for name, _ in collector.snapshot().phases]
    assert names[:2] == ["resolve", "mask"]
    assert len(names) == len(set(names))


def test_resolve_and_publish_never_mark_tables():
    collector = RunResultCollector()
    collector.on_event(_phase("resolve", "start"))
    collector.on_event(_phase("resolve", "error", elapsed_s=0.1))
    facts = collector.snapshot()
    assert facts.aggregate_error is False
    cfg = {
        "version": 2,
        "tables": [{"name": "t1", "columns": [{"name": "a", "strategy": "hash"}]}],
        "sources": {"t1": {"type": "file", "format": "csv", "path": "x.csv"}},
    }
    rows = project_tables(
        config=cfg, execution_result=None, facts=facts, delivery=None, run_failed=True
    ).tables
    assert [r.outcome for r in rows] == ["not_run"]

    collector.on_event(_phase("publish", "start", "t1"))
    collector.on_event(_phase("publish", "finish", "t1", elapsed_s=0.2))
    after = collector.snapshot()
    assert not after.started and not after.finished and not after.errored


def test_rule_table_for_synthetic_events():
    collector = RunResultCollector()
    collector.on_event(_phase("mask", "start"))
    collector.on_event(_phase("mask", "finish", rows_out=9, elapsed_s=0.1))
    assert collector.snapshot() == CollectedFacts(phases=(("mask", 0.1),))
    collector.on_event(_phase("mask", "error", elapsed_s=0.1))
    assert collector.snapshot().aggregate_error is True
    collector.on_event(RateSample("t1", 2, 1.0, 1.0))
    assert "t1" in collector.snapshot().started and not collector.snapshot().rate_rows
    collector.on_event(RateSample("t1", 2, 1.0, 1.0, final=True))
    collector.on_event(RateSample("t1", 7, 1.0, 1.0, final=True))
    assert collector.snapshot().rate_rows == {"t1": 7}
    collector.on_event(_phase("mask", "finish", "t2", elapsed_s=0.0))
    assert dict(collector.snapshot().finished) == {"t2": None}
    collector.on_event(_phase("finalize", "finish", "t3", rows_out=1, elapsed_s=0.0))
    assert "t3" not in collector.snapshot().finished


def _three_tables() -> dict:
    cols = [{"name": "a", "strategy": "hash"}]
    return {
        "version": 2,
        "tables": [{"name": n, "columns": cols} for n in ("a", "b", "c")],
        "sources": {n: {"type": "file", "format": "csv", "path": f"{n}.csv"} for n in "abc"},
    }


def _outcomes(collector: RunResultCollector) -> list[str]:
    rows = project_tables(
        config=_three_tables(),
        execution_result=None,
        facts=collector.snapshot(),
        delivery=None,
        run_failed=True,
    ).tables
    return [r.outcome for r in rows]


def test_refused_table_name_on_a_per_table_route_marks_no_table():
    collector = RunResultCollector()
    collector.on_event(RouteEvent("sequential"))
    collector.on_event(_phase("mask", "start", "a"))
    collector.on_event(_phase("mask", "finish", "a", rows_out=3, elapsed_s=0.1))
    # PhaseEvent drops a table name that approve_name refuses, so the error arrives table-less.
    collector.on_event(_phase("mask", "start", "orders@2024"))
    collector.on_event(_phase("mask", "error", "orders@2024", elapsed_s=0.1))
    assert collector.snapshot().aggregate_error is False
    assert _outcomes(collector) == ["succeeded", "not_run", "not_run"]


@pytest.mark.parametrize("route", ["full_frame", "mixed"])
def test_table_less_mask_error_on_a_whole_run_route_fails_the_run(route: str):
    collector = RunResultCollector()
    collector.on_event(RouteEvent(route))
    collector.on_event(_phase("mask", "start"))
    collector.on_event(_phase("mask", "error", elapsed_s=0.1))
    assert collector.snapshot().aggregate_error is True
    assert _outcomes(collector) == ["failed", "failed", "failed"]


def test_a_phase_name_outside_the_vocabulary_is_timed_as_unknown():
    collector = RunResultCollector()
    collector.on_event(_phase("zz_new_phase", "finish", "t1", rows_out=4, elapsed_s=1.5))
    facts = collector.snapshot()
    assert facts.phases == (("unknown", 1.5),)
    assert not facts.finished


def test_out_of_core_reorder_driver_counts_rows(tmp_path: Path):
    cfg, sources = _job(tmp_path, ("t1", "t2"), fk=True)
    collector = RunResultCollector()
    run_pipeline(
        cfg,
        sources=sources,
        engine_version=_VERSION,
        progress=collector,
        execution_mode="out_of_core",
        sink=ParquetTransactionalSink(tmp_path / "sink"),
        out_of_core_reorder_threshold_rows=0,
        out_of_core_budget_bytes=512 * 1024 * 1024,
    )
    tables = _project(cfg, collector)
    for name in ("t1", "t2"):
        assert (tables[name].rows_out, tables[name].rows_source) == (3, "phase")


@pytest.mark.parametrize("route", ["sequential", "out_of_core"])
def test_a_table_that_fails_while_loading_reads_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
):
    cfg, sources = _job(tmp_path, ("t1", "t2", "t3"), fk=True)
    inputs: dict[str, Any] = {"sources": sources}
    if route == "sequential":

        def loader(name: str) -> pa.Table:
            if name == "t2":
                raise RuntimeError("load failed")
            return sources[name]

        inputs = {
            "source_loader": loader,
            "source_loader_schemas": {n: t.schema for n, t in sources.items()},
        }
    else:
        from decoy_engine.execution.out_of_core import _runner

        real, calls = _runner.resolve_incoming, []

        def resolve(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("load failed")
            return real(*args, **kwargs)

        monkeypatch.setattr(_runner, "resolve_incoming", resolve)
    collector = RunResultCollector()
    with pytest.raises(RuntimeError, match="load failed"):
        run_pipeline(
            cfg, engine_version=_VERSION, progress=collector, execution_mode=route, **inputs
        )
    tables = _project(cfg, collector, run_failed=True)
    assert [tables[n].outcome for n in ("t1", "t2", "t3")] == ["succeeded", "failed", "not_run"]


_CHUNK_ROWS = (2, 3, 1)


def _chunks() -> Iterator[pa.Table]:
    for index, rows in enumerate(_CHUNK_ROWS):
        yield pa.table({"id": [f"c{index}-{i}" for i in range(rows)]})


@pytest.mark.parametrize(
    "entry",
    [
        run_mask_pipeline_chunked,
        pytest.param(
            run_mask_chunked,
            marks=pytest.mark.skipif(
                importlib.util.find_spec("decoy_engine_native") is None,
                reason="decoy-engine-native companion not installed; the companion job covers it",
            ),
        ),
    ],
    ids=["oracle", "native"],
)
def test_chunked_entries_count_rows(tmp_path: Path, entry: Any):
    cfg, _ = _job(tmp_path, ("t1",))
    collector = RunResultCollector()
    with use_progress(collector):
        received = sum(
            chunk.num_rows
            for chunk in entry(
                cfg,
                _chunks(),
                table="t1",
                engine_version=_VERSION,
                **chunked_entry_kwargs(entry, cfg, "t1"),
            )
        )
    row = _project(cfg, collector)["t1"]
    assert received == sum(_CHUNK_ROWS)
    assert (row.rows_out, row.rows_source, row.outcome) == (received, "phase", "succeeded")


class _Events:
    def __init__(self) -> None:
        self.phases: list[tuple[str, str | None]] = []
        self.collector = RunResultCollector()

    def on_event(self, event: Any) -> None:
        if isinstance(event, PhaseEvent):
            self.phases.append((event.status, event.table))
        self.collector.on_event(event)


def test_observe_chunked_outcomes():
    sink = _Events()
    with use_progress(sink):
        stream = observe_chunked("t1", _chunks())
        next(stream)
        stream.close()
    assert sink.phases == [("start", "t1"), ("finish", "t1")]
    facts = sink.collector.snapshot()
    assert dict(facts.finished) == {"t1": _CHUNK_ROWS[0]} and not facts.errored

    boom = RuntimeError("producer failed")

    def producer() -> Iterator[pa.Table]:
        yield from _chunks()
        raise boom

    failing = _Events()
    with use_progress(failing) as channel:
        with pytest.raises(RuntimeError) as raised, channel.run_scope():
            for _ in observe_chunked("t1", producer()):
                pass
    assert raised.value is boom
    assert [status for status, _ in failing.phases] == ["start", "error"]
    facts = failing.collector.snapshot()
    assert "t1" in facts.errored and "t1" not in facts.finished
