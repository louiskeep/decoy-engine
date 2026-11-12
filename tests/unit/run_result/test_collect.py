"""AT-K1 to AT-K4 and AT-K7: the progress collector, from synthetic events and from real runs."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.progress import PhaseEvent, RateSample
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.run_result import CollectedFacts, RunResultCollector, project_tables

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
        ("out_of_core", {"execution_mode": "out_of_core"}, "rate"),
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
    with pytest.raises(Exception):
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
    with pytest.raises(Exception):
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
