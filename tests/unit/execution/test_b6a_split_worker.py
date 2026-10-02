"""Acceptance tests 10 and 11 of plan 2026-10-02-b6a-incremental-output-sink (rev 2.1):
the B7 multi-table split streams only when every output is dispatched, and the isolated
worker publishes a streamed auto-chunk run end to end.

These tests are written before the implementation. Do not delete one, add a skip or
xfail, or relax a byte-identity comparison without a new plan gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _isolated_worker, run_pipeline, run_pipeline_isolated
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6a_support as b6a
from tests.unit.execution import _multi_table_support as mt


def _two_dispatched(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    return mt.build_job(
        tmp_path,
        {
            "a": (mt.std_columns("a_ns"), mt.string_table(mt.BIG, "a")),
            "b": (mt.std_columns("b_ns"), mt.string_table(mt.BIG, "b")),
        },
    )


def _run(cfg: dict[str, Any], sources: dict[str, pa.Table], sink: Any, **extra: Any) -> Any:
    return run_pipeline(cfg, sources=sources, sink=sink, **mt.kw(**extra))


def _reference(cfg: dict[str, Any], sources: dict[str, pa.Table]) -> Any:
    return run_pipeline(cfg, sources=sources, **mt.kw(**b6a.resident_kw()))


# ---------------------------------------------------------------------------
# Test 10: multi-table split.
# ---------------------------------------------------------------------------


def test_two_dispatched_tables_stream_in_config_order_with_per_table_evidence(
    tmp_path: Path,
) -> None:
    cfg, sources = _two_dispatched(tmp_path)
    expected = _reference(cfg, sources)
    sink, target = b6a.real_sink(tmp_path)
    result = _run(cfg, sources, sink)
    assert result.outputs == {}
    assert sink.calls == [("write_batches", "a"), ("write_batches", "b"), ("commit",)]
    for name in ("a", "b"):
        b6a.assert_streamed_equals(sink, expected.outputs[name], name)
        assert (target / f"{name}.parquet").read_bytes() == b6a.parquet_bytes(
            expected.outputs[name], tmp_path / f"ref_{name}.parquet"
        )
    assert sorted(p.name for p in target.iterdir()) == ["a.parquet", "b.parquet"]
    tables = {t["table"]: t for t in result.quality_metrics["auto_chunk"]["tables"]}
    assert set(tables) == {"a", "b"}
    for entry in tables.values():
        assert entry["output"]["mode"] == "streamed"
        assert entry["output"]["row_groups"] == 1
    assert result.quality_metrics["execution"]["outputs_streamed"] is True


def test_a_failure_in_the_second_table_aborts_once_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = _two_dispatched(tmp_path)

    def fail_in_b(table: str, index: int) -> None:
        if table == "b" and index == 1:
            raise RuntimeError("injected kernel failure")

    b6a.ChunkSpy(monkeypatch, before_pull=fail_in_b)
    sink, target = b6a.real_sink(tmp_path)
    with pytest.raises(RuntimeError, match="injected kernel failure"):
        _run(cfg, sources, sink)
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    assert not target.exists()
    assert b6a.leftovers(tmp_path) == []


def _date_table(n: int) -> pa.Table:
    return pa.table({"d": pa.array([f"2020-01-{1 + i % 28:02d}" for i in range(n)])})


_DATE_NO_FORMAT = [
    {
        "name": "d",
        "strategy": "date_shift",
        "namespace": "d_ns",
        "provider_config": {"min_days": -3, "max_days": 3},
    }
]


def _sibling_job(kind: str, tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table], str]:
    tables: dict[str, mt.TableSpec] = {"a": (mt.std_columns("a_ns"), mt.string_table(mt.BIG, "a"))}
    extra: dict[str, pa.Table] = {}
    if kind == "full_frame_small":
        tables["c"] = (mt.std_columns("c_ns"), mt.string_table(mt.SMALL, "c"))
        reason = "split_full_frame_present"
    elif kind == "full_frame_large_date_shift":
        tables["c"] = (_DATE_NO_FORMAT, _date_table(mt.BIG))
        reason = "split_full_frame_present"
    else:
        tables["b"] = (mt.std_columns("b_ns"), mt.string_table(mt.BIG, "b"))
        extra["lookup"] = pa.table({"k": pa.array(["x", "y"])})
        reason = "split_extra_sources_present"
    cfg, sources = mt.build_job(tmp_path, tables)
    return cfg, {**sources, **extra}, reason


@pytest.mark.parametrize(
    "kind", ["full_frame_small", "full_frame_large_date_shift", "extra_resident_frame"]
)
def test_a_split_with_a_resident_group_or_extra_frame_stays_resident(
    kind: str, tmp_path: Path
) -> None:
    cfg, sources, reason = _sibling_job(kind, tmp_path)
    expected = _reference(cfg, sources)
    sink = b6a.RecordingSink()
    result = _run(cfg, sources, sink)
    assert sink.calls == []
    assert list(result.outputs) == list(expected.outputs)
    for name in result.outputs:
        assert result.outputs[name].equals(expected.outputs[name], check_metadata=True), name
    assert result.quality_metrics["execution"]["outputs_streamed"] is False
    dispatched = [t for t in result.quality_metrics["auto_chunk"]["tables"] if t["dispatched"]]
    assert dispatched, "the job must still split"
    for entry in dispatched:
        assert entry["output"] == {"mode": "resident", "reason": reason}


def test_a_group_row_error_leaves_the_sink_untouched(tmp_path: Path) -> None:
    bad = pa.table({"d": pa.array(["2020-01-01"] * (mt.BIG - 1) + ["not-a-date"])})
    cols = [
        {
            "name": "d",
            "strategy": "date_shift",
            "namespace": "d_ns",
            "provider_config": {"min_days": -5, "max_days": 5},
        }
    ]
    cfg, sources = mt.build_job(
        tmp_path,
        {"a": (mt.std_columns("a_ns"), mt.string_table(mt.BIG, "a")), "c": (cols, bad)},
    )
    sink = b6a.RecordingSink()
    with pytest.raises(RowErrorsFailedError):
        _run(cfg, sources, sink)
    assert sink.calls == []


# ---------------------------------------------------------------------------
# Test 5 (split half): no dispatched output is alive when the next table starts.
# ---------------------------------------------------------------------------


def test_no_dispatched_output_is_alive_when_the_next_table_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = _two_dispatched(tmp_path)
    alive_at_start: dict[str, int] = {}
    holder: dict[str, b6a.ChunkSpy] = {}

    def before_pull(table: str, index: int) -> None:
        if index == 0 and table != "a":
            alive_at_start[table] = holder["spy"].alive("a")

    holder["spy"] = b6a.ChunkSpy(monkeypatch, before_pull=before_pull)
    sink = b6a.RecordingSink()
    _run(cfg, sources, sink)
    assert sink.count("commit") == 1
    assert alive_at_start == {"b": 0}


# ---------------------------------------------------------------------------
# Test 11: isolated worker end to end.
# ---------------------------------------------------------------------------

_WORKER_ROWS = 200_000


def _worker_job(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(
        {
            "r": pa.array([f"s{i}" for i in range(_WORKER_ROWS)]),
            "p": pa.array(range(_WORKER_ROWS), pa.int64()),
        }
    )
    cfg = support.make_cfg(
        [support.redact_col("r"), support.pass_col("p")],
        path=support.write_source(src, tmp_path / "src.parquet"),
    )
    return cfg, src


def _payload(cfg: dict[str, Any], tmp_path: Path, name: str, **kwargs: Any) -> dict[str, Any]:
    return {
        "config": cfg,
        "sources": {"t": cfg["sources"]["t"]["path"]},
        "kwargs": {
            "engine_version": "b6a-worker",
            "auto_chunk_threshold_rows": 1_000,
            "chunk_size_rows": 50_000,
            **kwargs,
        },
        "mem_cap_bytes": None,
        "rlimit_kind": "data",
        "staging_output_dir": str(tmp_path / name),
    }


def test_the_isolated_worker_streams_a_routed_job_and_matches_the_resident_bytes(
    tmp_path: Path,
) -> None:
    cfg, _src = _worker_job(tmp_path)
    streamed = _isolated_worker._run(_payload(cfg, tmp_path, "streamed"))
    resident = _isolated_worker._run(_payload(cfg, tmp_path, "resident", **b6a.resident_kw()))
    assert streamed["outcome"] == resident["outcome"] == "completed"
    assert streamed["quality_metrics"]["execution"]["outputs_streamed"] is True
    assert resident["quality_metrics"]["execution"]["outputs_streamed"] is False
    assert streamed["staged_tables"] == ["t"] == resident["staged_tables"]
    assert (tmp_path / "streamed" / "t.parquet").read_bytes() == (
        tmp_path / "resident" / "t.parquet"
    ).read_bytes()


def test_run_pipeline_isolated_returns_equal_outputs_and_publishes_the_same_bytes(
    tmp_path: Path,
) -> None:
    cfg, src = _worker_job(tmp_path)
    kwargs = {
        "engine_version": "b6a-worker",
        "auto_chunk_threshold_rows": 1_000,
        "chunk_size_rows": 50_000,
    }
    streamed = run_pipeline_isolated(
        cfg, {"t": src}, output_dir=tmp_path / "pub_streamed", **kwargs
    )
    resident = run_pipeline_isolated(
        cfg, {"t": src}, output_dir=tmp_path / "pub_resident", **b6a.resident_kw(), **kwargs
    )
    assert streamed.outcome == resident.outcome == "completed"
    assert streamed.quality_metrics["execution"]["outputs_streamed"] is True
    assert streamed.outputs["t"].equals(resident.outputs["t"], check_metadata=True)
    assert (tmp_path / "pub_streamed" / "t.parquet").read_bytes() == (
        tmp_path / "pub_resident" / "t.parquet"
    ).read_bytes()
    assert pq.read_table(tmp_path / "pub_streamed" / "t.parquet").num_rows == _WORKER_ROWS
