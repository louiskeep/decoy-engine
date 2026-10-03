"""Acceptance tests 5, 7 and 8 of plan 2026-10-02-b6b-lazy-batch-input (rev 2.1): where each
lazy table is resolved, the B7 split with lazy tables, and the isolated worker.

These tests are written before the implementation. Do not delete one, add a skip or xfail,
or relax a comparison without a new plan gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import _isolated_worker, run_pipeline_isolated
from decoy_engine.profile._readers import LazySource
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6b_support as b6b
from tests.unit.execution import _transform_testkit as tk
from tests.unit.execution.test_b6a_publish import _resident_case

RESIDENT_REASONS = [
    "streaming_disabled",
    "no_sink",
    "sink_not_streaming",
    "legacy_lane",
    "validators_present",
    "quarantine_enabled",
    "fidelity_report",
    "post_validation",
]


def _lazy_of_cfg(cfg: dict[str, Any]) -> LazySource:
    return b6b.lazy(cfg["sources"][b6b.TABLE]["path"])


# ---------------------------------------------------------------------------
# Test 5: input mode and materialization.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", RESIDENT_REASONS)
def test_a_routed_lazy_table_with_a_resident_output_is_materialized_once_before_the_lane(
    reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline_finalize

    cfg, kwargs, sink = _resident_case(reason, tmp_path)
    kwargs["now_iso"] = "2026-01-01T00:00:00+00:00"
    handed = {b6b.TABLE: _lazy_of_cfg(cfg)}
    twin_sink = b6b.b6a.RecordingSink() if isinstance(sink, b6b.b6a.RecordingSink) else sink
    want = b6b.run(cfg, b6b.twin_sources(handed), twin_sink, **kwargs)
    order: list[str] = []
    real_to_table = LazySource.to_table
    monkeypatch.setattr(
        LazySource, "to_table", lambda self: (order.append("to_table"), real_to_table(self))[1]
    )
    lanes = support.spy_lanes(monkeypatch)
    kinds: dict[str, list[Any]] = {"validators": [], "fidelity": [], "post": []}
    real_val = _pipeline_finalize.finalize_validators_and_quarantine
    real_fid = _pipeline_finalize.compute_fidelity_reports
    real_post = _pipeline_finalize.compute_post_validation

    def val(outputs: Any, **kw: Any) -> Any:
        kinds["validators"].append(kw["caller_sources"])
        return real_val(outputs, **kw)

    def fid(outputs: Any, merged: Any, **kw: Any) -> Any:
        kinds["fidelity"].append(merged)
        return real_fid(outputs, merged, **kw)

    def post(result: Any, **kw: Any) -> Any:
        kinds["post"].append(kw["sources"])
        return real_post(result, **kw)

    monkeypatch.setattr(_pipeline_finalize, "finalize_validators_and_quarantine", val)
    monkeypatch.setattr(_pipeline_finalize, "compute_fidelity_reports", fid)
    monkeypatch.setattr(_pipeline_finalize, "compute_post_validation", post)
    if lanes["auto_chunk.run_auto_chunk"] is not None:
        from decoy_engine.execution import _pipeline_auto_chunk as ac

        real_lane = ac.run_auto_chunk
        monkeypatch.setattr(
            ac, "run_auto_chunk", lambda *a, **k: (order.append("lane"), real_lane(*a, **k))[1]
        )
    got = b6b.run(cfg, handed, sink, **kwargs)
    assert order.count("to_table") == 1, order
    assert "lane" not in order or order.index("to_table") < order.index("lane")
    for held in (*kinds["validators"], *kinds["fidelity"], *kinds["post"]):
        assert all(isinstance(v, pa.Table) for v in held.values()), held
    assert list(got.outputs) == list(want.outputs)
    for name in want.outputs:
        assert got.outputs[name].equals(want.outputs[name], check_metadata=True), name
    assert b6b.strip_input_leaves(got.quality_metrics) == b6b.strip_input_leaves(
        want.quality_metrics
    )
    block = b6b.input_block(got)
    out_reason = got.quality_metrics["auto_chunk"]["output"]["reason"]
    assert block == {"mode": "resident", "reason": out_reason} and out_reason == reason


@support.NEEDS_COMPANION  # asserts unified_slice_activation, stamped only on the admitted native path
def test_a_lazy_table_below_the_threshold_is_resolved_before_the_unified_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = pa.table(
        {"r": pa.array([f"s{i}" for i in range(5)]), "x": pa.array(range(5), pa.int64())}
    )
    path = b6b.write(src, tmp_path / "s.parquet")
    cfg = support.make_cfg([support.redact_col("r"), support.pass_col("x")], path=path)
    handed = {b6b.TABLE: b6b.lazy(path)}
    twin = b6b.run(cfg, b6b.twin_sources(handed))
    seen: list[Any] = []
    from decoy_engine.execution import _unified_slice

    real = _unified_slice.maybe_run_unified_slice

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs["caller_sources"])
        return real(**kwargs)

    monkeypatch.setattr(_unified_slice, "maybe_run_unified_slice", spy)
    got = b6b.run(cfg, handed)
    assert len(seen) == 1 and all(isinstance(v, pa.Table) for v in seen[0].values())
    assert "unified_slice_activation" in got.quality_metrics
    assert (
        got.quality_metrics["unified_slice_activation"]
        == twin.quality_metrics["unified_slice_activation"]
    )
    assert got.outputs[b6b.TABLE].equals(twin.outputs[b6b.TABLE], check_metadata=True)


def test_a_transform_bearing_lazy_table_is_transformed_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline_sources, _transforms_table

    table = pa.table(
        {"s": pa.array([f"s{i}" for i in range(30)]), "x": pa.array(range(30), pa.int64())}
    )
    cfg = tk.single_table_config(
        tmp_path, table, transforms=[{"op": "filter", "expression": "x > 4"}]
    )
    handed = {"t": b6b.lazy(cfg["sources"]["t"]["path"])}
    want = b6b.run(cfg, b6b.twin_sources(handed), b6b.b6a.RecordingSink())
    calls: list[str] = []
    for owner in (_transforms_table, _pipeline_sources):
        real = owner.transform_resolved_source
        monkeypatch.setattr(
            owner,
            "transform_resolved_source",
            lambda *a, real=real, **k: (calls.append("t"), real(*a, **k))[1],
        )
    got = b6b.run(cfg, handed, b6b.b6a.RecordingSink())
    assert len(calls) == 1, calls
    for name in want.outputs:
        assert got.outputs[name].equals(want.outputs[name], check_metadata=True)
    assert got.outputs["t"].num_rows == 25


# ---------------------------------------------------------------------------
# Test 7: B7 split with lazy tables.
# ---------------------------------------------------------------------------


def _two_tables(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    native = [support.redact_col("h"), support.redact_col("r")]
    return b6b.mt.build_job(
        tmp_path,
        {
            "a": (native, b6b.mt.string_table(b6b.mt.BIG, "a")),
            "b": (native, b6b.mt.string_table(b6b.mt.BIG, "b")),
        },
    )


def _handed(cfg: dict[str, Any], names: list[str], resident: dict[str, pa.Table]) -> dict[str, Any]:
    return {
        name: b6b.lazy(cfg["sources"][name]["path"]) if name in names else resident[name]
        for name in resident
    }


def _split_run(
    cfg: dict[str, Any], sources: dict[str, Any], tmp_path: Path, tag: str
) -> tuple[Any, Any, Path]:
    sink, target = b6b.b6a.real_sink(tmp_path, tag)
    return b6b.run_pipeline(cfg, sources=sources, sink=sink, **b6b.mt.kw()), sink, target


def test_two_lazy_tables_publish_the_twins_files_and_open_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, resident = _two_tables(tmp_path)
    handed = _handed(cfg, ["a", "b"], resident)
    want, _wsink, wtarget = _split_run(cfg, resident, tmp_path, "twin")
    mod = b6b.chunked_input()
    events: list[tuple[str, str]] = []
    real_open = mod.open_input
    monkeypatch.setattr(
        mod,
        "open_input",
        lambda source, **kw: (events.append(("open", kw["table"])), real_open(source, **kw))[1],
    )
    sink, ltarget = b6b.b6a.real_sink(tmp_path, "lazy")
    sink.on_batch = lambda table, _batch: events.append(("batch", table))
    got = b6b.run_pipeline(cfg, sources=handed, sink=sink, **b6b.mt.kw())
    for name in ("a", "b"):
        assert (ltarget / f"{name}.parquet").read_bytes() == (
            wtarget / f"{name}.parquet"
        ).read_bytes()
        assert b6b.input_block(got, name)["mode"] == "lazy"
    assert got.quality_metrics["execution"]["loaded_fully_in_memory"] is False
    opens = [i for i, e in enumerate(events) if e[0] == "open"]
    last_a = max(i for i, e in enumerate(events) if e == ("batch", "a"))
    assert [events[i][1] for i in opens] == ["a", "b"]
    assert opens[1] > last_a, events


def test_a_lazy_and_a_resident_table_both_stream_and_the_run_is_not_fully_lazy(
    tmp_path: Path,
) -> None:
    cfg, resident = _two_tables(tmp_path)
    handed = _handed(cfg, ["a"], resident)
    want, _s, wtarget = _split_run(cfg, resident, tmp_path, "twin")
    got, _sink, ltarget = _split_run(cfg, handed, tmp_path, "mixed")
    for name in ("a", "b"):
        assert (ltarget / f"{name}.parquet").read_bytes() == (
            wtarget / f"{name}.parquet"
        ).read_bytes()
    assert b6b.input_block(got, "a")["mode"] == "lazy"
    assert b6b.input_block(got, "b") == {"mode": "resident", "reason": "resident_source"}
    assert got.quality_metrics["execution"]["loaded_fully_in_memory"] is True


def test_a_split_with_a_full_frame_group_materializes_each_lazy_table_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = [support.redact_col("h"), support.redact_col("r")]
    cfg, resident = b6b.mt.build_job(
        tmp_path,
        {
            "a": (native, b6b.mt.string_table(b6b.mt.BIG, "a")),
            "c": (native, b6b.mt.string_table(b6b.mt.SMALL, "c")),
        },
    )
    handed = {name: b6b.lazy(cfg["sources"][name]["path"]) for name in resident}
    calls: list[Path] = []
    real = LazySource.to_table
    monkeypatch.setattr(
        LazySource, "to_table", lambda self: (calls.append(self.path), real(self))[1]
    )
    sink = b6b.b6a.RecordingSink()
    got = b6b.run_pipeline(cfg, sources=handed, sink=sink, **b6b.mt.kw())
    assert sorted(p.name for p in calls) == ["a.parquet", "c.parquet"]
    assert sink.calls == []
    assert b6b.input_block(got, "a") == {"mode": "resident", "reason": "split_full_frame_present"}
    want = b6b.run_pipeline(cfg, sources=resident, sink=b6b.b6a.RecordingSink(), **b6b.mt.kw())
    for name in want.outputs:
        assert got.outputs[name].equals(want.outputs[name], check_metadata=True), name


def test_a_read_failure_in_the_second_table_aborts_once_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, resident = _two_tables(tmp_path)
    handed = _handed(cfg, ["a", "b"], resident)
    real_init = pq.ParquetFile.__init__
    real_iter = pq.ParquetFile.iter_batches

    def init(self: Any, source: Any, *args: Any, **kwargs: Any) -> None:
        self._b6b_path = str(source)
        real_init(self, source, *args, **kwargs)

    def iter_batches(self: Any, *args: Any, **kwargs: Any) -> Any:
        for index, batch in enumerate(real_iter(self, *args, **kwargs)):
            if self._b6b_path.endswith("b.parquet") and index == 1:
                raise OSError("injected read failure")
            yield batch

    monkeypatch.setattr(pq.ParquetFile, "__init__", init)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", iter_batches)
    sink, target = b6b.b6a.real_sink(tmp_path, "lazy")
    with pytest.raises(OSError, match="injected read failure"):
        b6b.run_pipeline(cfg, sources=handed, sink=sink, **b6b.mt.kw())
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    assert not target.exists()
    assert b6b.b6a.leftovers(tmp_path) == []


# ---------------------------------------------------------------------------
# Test 8: isolated worker.
# ---------------------------------------------------------------------------

_ROWS = 200_000


def _worker_job(tmp_path: Path, rows: int, cols: int = 2) -> dict[str, Any]:
    data: dict[str, pa.Array] = {"r": pa.array([f"s{i}" for i in range(rows)])}
    for c in range(cols - 2):
        data[f"c{c}"] = pa.array([f"v{c}-{i}" for i in range(rows)])
    data["p"] = pa.array(range(rows), pa.int64())
    path = b6b.write(pa.table(data), tmp_path / "src.parquet")
    columns = [support.redact_col("r")] + [support.redact_col(f"c{c}") for c in range(cols - 2)]
    columns.append(support.pass_col("p"))
    return support.make_cfg(columns, path=path)


def _payload(cfg: dict[str, Any], tmp_path: Path, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "config": cfg,
        "sources": {"t": cfg["sources"]["t"]["path"]},
        "kwargs": {
            "engine_version": "b6b-worker",
            "auto_chunk_threshold_rows": 1_000,
            "chunk_size_rows": 50_000,
            **extra,
        },
        "mem_cap_bytes": None,
        "rlimit_kind": "data",
        "staging_output_dir": str(tmp_path / name),
    }


def test_the_worker_hands_run_pipeline_lazy_sources_for_a_plain_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _worker_job(tmp_path, 100)
    seen: list[Any] = []
    real = _isolated_worker.run_pipeline

    def spy(config: Any, sources: Any, **kwargs: Any) -> Any:
        seen.append(dict(sources))
        return real(config, sources, **kwargs)

    monkeypatch.setattr(_isolated_worker, "run_pipeline", spy)
    envelope = _isolated_worker._run(_payload(cfg, tmp_path, "out"))
    assert envelope["outcome"] == "completed"
    assert len(seen) == 1 and all(isinstance(v, LazySource) for v in seen[0].values())


def _eager_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _isolated_worker._load_sources
    monkeypatch.setattr(
        _isolated_worker, "_load_sources", lambda manifest, lazy=False: real(manifest, lazy=False)
    )


def test_a_routed_worker_job_streams_and_matches_the_eager_envelope_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _worker_job(tmp_path, _ROWS)
    lazy_env = _isolated_worker._run(_payload(cfg, tmp_path, "lazy_out"))
    _eager_worker(monkeypatch)
    eager_env = _isolated_worker._run(_payload(cfg, tmp_path, "eager_out"))
    assert lazy_env["outcome"] == eager_env["outcome"] == "completed"
    assert lazy_env["quality_metrics"]["execution"]["outputs_streamed"] is True
    assert lazy_env["quality_metrics"]["auto_chunk"]["input"]["mode"] == "lazy"
    assert eager_env["quality_metrics"]["auto_chunk"]["input"] == {
        "mode": "resident",
        "reason": "resident_source",
    }
    assert lazy_env["staged_tables"] == eager_env["staged_tables"] == ["t"]
    assert (tmp_path / "lazy_out" / "t.parquet").read_bytes() == (
        tmp_path / "eager_out" / "t.parquet"
    ).read_bytes()


@support.NEEDS_COMPANION  # asserts unified_slice_activation, stamped only on the admitted native path
def test_a_small_worker_job_equals_the_eager_run_including_unified_slice_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _worker_job(tmp_path, 1_000)
    lazy_env = _isolated_worker._run(
        _payload(cfg, tmp_path, "lazy_out", auto_chunk_threshold_rows=100_000)
    )
    _eager_worker(monkeypatch)
    eager_env = _isolated_worker._run(
        _payload(cfg, tmp_path, "eager_out", auto_chunk_threshold_rows=100_000)
    )
    assert lazy_env["outcome"] == eager_env["outcome"] == "completed"
    assert "unified_slice_activation" in lazy_env["quality_metrics"]
    assert b6b.mt.strip_elapsed(lazy_env["quality_metrics"]) == b6b.mt.strip_elapsed(
        eager_env["quality_metrics"]
    )
    assert (tmp_path / "lazy_out" / "t.parquet").read_bytes() == (
        tmp_path / "eager_out" / "t.parquet"
    ).read_bytes()


def test_a_single_table_job_completes_under_a_low_memory_cap(tmp_path: Path) -> None:
    cfg = _worker_job(tmp_path, _ROWS, cols=8)
    result = run_pipeline_isolated(
        cfg,
        {"t": pq.read_table(cfg["sources"]["t"]["path"])},
        output_dir=tmp_path / "published",
        engine_version="b6b-worker",
        auto_chunk_threshold_rows=1_000,
        chunk_size_rows=50_000,
        mem_cap_bytes=768 * 1024 * 1024,
        rlimit_kind="data",
    )
    assert result.outcome == "completed", result.error
    assert pq.read_table(tmp_path / "published" / "t.parquet").num_rows == _ROWS
