"""`run_pipeline(progress=...)` end to end (parity E1b-E1d): every route emits
its phases and rate samples, output bytes are identical with and without a
sink, a raising sink is isolated, and no cell value reaches any event."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import progress
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline

_ENGINE_VERSION = "progress-e2e"
_SECRET = "ZQXJV-SECRET-VALUE"


class _Recorder:
    def __init__(self) -> None:
        self.events: list[progress.ProgressEvent] = []

    def on_event(self, event: progress.ProgressEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def phases(self) -> list[tuple[str, str, str | None]]:
        return [(e.name, e.status, e.table) for e in self.events if e.kind == "phase"]

    def dump(self) -> str:
        return json.dumps([progress.event_to_dict(e) for e in self.events])


def _dump(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _scalar_job(tmp_path: Path, rows: int = 3):
    df = pd.DataFrame(
        {"email": [f"{_SECRET}-{i}@x.com" for i in range(rows)], "zip": ["90210"] * rows}
    )
    src = tmp_path / "c.csv"
    df.to_csv(src, index=False)
    cfg = _dump(
        {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {"customers": {"type": "file", "format": "csv", "path": str(src)}},
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {"name": "email", "strategy": "hash", "namespace": "e"},
                        {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3}},
                    ],
                }
            ],
            "targets": {
                "customers": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}
            },
        }
    )
    return cfg, {"customers": pa.Table.from_pandas(df, preserve_index=False)}


def _fk_job(tmp_path: Path):
    customers = pd.DataFrame({"id": [f"{_SECRET}-C{i}" for i in range(4)]})
    orders = pd.DataFrame({"customer_id": [f"{_SECRET}-C1", f"{_SECRET}-C2", f"{_SECRET}-C1"]})
    cp, op = tmp_path / "customers.csv", tmp_path / "orders.csv"
    customers.to_csv(cp, index=False)
    orders.to_csv(op, index=False)
    cfg = _dump(
        {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {"type": "file", "format": "csv", "path": str(cp)},
                "orders": {"type": "file", "format": "csv", "path": str(op)},
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "id_ns"}],
                },
                {
                    "name": "orders",
                    "columns": [{"name": "customer_id", "strategy": "hash", "namespace": "id_ns"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "preserve",
                    "namespace": "id_ns",
                }
            ],
            "targets": {
                "customers": {"type": "file", "format": "csv", "path": str(tmp_path / "c_out.csv")},
                "orders": {"type": "file", "format": "csv", "path": str(tmp_path / "o_out.csv")},
            },
        }
    )
    return cfg, {
        "customers": pa.Table.from_pandas(customers, preserve_index=False),
        "orders": pa.Table.from_pandas(orders, preserve_index=False),
    }


def _generate_job(tmp_path: Path, rows: int = 50):
    cfg = _dump(
        {
            "version": 2,
            "global_settings": {"seed": 7},
            "sources": {},
            "tables": [
                {
                    "name": "extra",
                    "row_count": rows,
                    "generate_columns": [
                        {"name": "seq", "type": "sequence", "start": 1, "step": 1}
                    ],
                }
            ],
            "targets": {
                "extra": {"type": "file", "format": "csv", "path": str(tmp_path / "extra.csv")}
            },
        }
    )
    return cfg, {}


def test_full_frame_events_and_byte_identity(tmp_path: Path) -> None:
    cfg, sources = _scalar_job(tmp_path)
    plain = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
    rec = _Recorder()
    observed = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, progress=rec)
    assert observed.outputs["customers"].equals(plain.outputs["customers"])
    assert rec.phases()[0] == ("resolve", "start", None)
    assert ("resolve", "finish", None) in rec.phases()
    assert "route" in rec.kinds()
    assert rec.events[-1].kind == "terminal" and rec.events[-1].status == "ok"
    assert _SECRET not in rec.dump()


def test_auto_chunk_lane_emits_rate_samples(tmp_path: Path) -> None:
    cfg, sources = _scalar_job(tmp_path, rows=120)
    rec = _Recorder()
    result = run_pipeline(
        cfg,
        sources=sources,
        engine_version=_ENGINE_VERSION,
        progress=rec,
        auto_chunk_threshold_rows=10,
        chunk_size_rows=25,
    )
    assert result.outputs["customers"].num_rows == 120
    samples = [e for e in rec.events if e.kind == "rate" and e.table == "customers"]
    assert samples and samples[-1].final and samples[-1].rows_done == 120
    route = next(e for e in rec.events if e.kind == "route")
    assert route.route == "full_frame" and route.lane == "chunked"
    assert _SECRET not in rec.dump()


def test_sequential_route_emits_per_table_phases(tmp_path: Path) -> None:
    cfg, sources = _fk_job(tmp_path)
    rec = _Recorder()
    plain = run_pipeline(
        cfg, sources=sources, engine_version=_ENGINE_VERSION, execution_mode="sequential"
    )
    observed = run_pipeline(
        cfg,
        sources=sources,
        engine_version=_ENGINE_VERSION,
        execution_mode="sequential",
        progress=rec,
    )
    for t in ("customers", "orders"):
        assert observed.outputs[t].equals(plain.outputs[t])
    tables = [
        e.table
        for e in rec.events
        if e.kind == "phase" and e.name == "mask" and e.status == "finish"
    ]
    assert tables == ["customers", "orders"]
    finish = next(
        e for e in rec.events if e.kind == "phase" and e.table == "orders" and e.status == "finish"
    )
    assert finish.rows_in == 3 and finish.rows_out == 3
    assert next(e for e in rec.events if e.kind == "route").route == "sequential"
    assert _SECRET not in rec.dump()


def test_out_of_core_route_emits_per_table_phases_and_rows(tmp_path: Path) -> None:
    cfg, sources = _fk_job(tmp_path)
    rec = _Recorder()
    result = run_pipeline(
        cfg,
        sources=sources,
        engine_version=_ENGINE_VERSION,
        execution_mode="out_of_core",
        progress=rec,
    )
    assert set(result.outputs) >= {"customers", "orders"}
    tables = [
        e.table
        for e in rec.events
        if e.kind == "phase" and e.name == "mask" and e.status == "finish"
    ]
    assert tables == ["customers", "orders"]
    samples = [e for e in rec.events if e.kind == "rate"]
    assert {s.table for s in samples} == {"customers", "orders"}
    assert _SECRET not in rec.dump()


def test_generation_emits_per_table_phase_and_rate(tmp_path: Path) -> None:
    cfg, sources = _generate_job(tmp_path)
    rec = _Recorder()
    plain = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
    observed = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, progress=rec)
    assert observed.outputs["extra"].equals(plain.outputs["extra"])
    gen = [e for e in rec.events if e.kind == "phase" and e.name == "generate"]
    assert [(e.status, e.table) for e in gen] == [("start", "extra"), ("finish", "extra")]
    assert gen[1].rows_out == 50
    samples = [e for e in rec.events if e.kind == "rate" and e.table == "extra"]
    assert samples and samples[-1].rows_done == 50 and samples[-1].final


def test_raising_sink_does_not_change_the_result(tmp_path: Path) -> None:
    cfg, sources = _scalar_job(tmp_path)
    plain = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)

    class Raising:
        def on_event(self, event):
            raise RuntimeError("boom")

    observed = run_pipeline(
        cfg, sources=sources, engine_version=_ENGINE_VERSION, progress=Raising()
    )
    assert observed.outputs["customers"].equals(plain.outputs["customers"])


def test_failure_emits_terminal_error_without_the_message(tmp_path: Path) -> None:
    cfg, sources = _scalar_job(tmp_path)
    rec = _Recorder()
    with pytest.raises(Exception):
        run_pipeline(
            cfg, sources=sources, engine_version=_ENGINE_VERSION, progress=rec, fpe_chunk_count=0
        )
    assert rec.events[-1].kind == "terminal" and rec.events[-1].status == "error"
    assert rec.events[-1].error_class == "ExecutionError"


def test_isolated_run_refuses_a_progress_sink(tmp_path: Path) -> None:
    from decoy_engine.execution import run_pipeline_isolated

    cfg, sources = _scalar_job(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, progress=_Recorder())


def test_invalid_progress_sink_is_a_type_error(tmp_path: Path) -> None:
    cfg, sources = _scalar_job(tmp_path)
    with pytest.raises(TypeError):
        run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, progress=object())
