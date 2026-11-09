"""AT-E1a (RUN-STREAM E1a): `execution_mode="stream"` is "must stream".

Plan: docs/plans/2026-11-08-run-stream.md sections 5.3 and 8. `stream` is translated once to
`auto` plus an immutable `stream_required`; a strict preflight reads config, plan and footers and
refuses with `runtime.streaming_unavailable` before any source data row is read; the size
threshold does not apply; streamed output keeps base `--chunked` row groups; FK jobs split per
table when every table passes the complete chunked gate, else take the guarded out-of-core route.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons
from decoy_engine.execution import _pipeline_context, run_pipeline
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.profile._readers import LazySource
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution.run_stream import _support as rs

# (rows, chunk_size_rows or None for the default 50,000): empty, one short chunk, the default
# chunk over a table of more than two chunks, and a chunk larger than the writer cap.
CASES = [
    pytest.param(0, 16, id="empty"),
    pytest.param(10, 16, id="ten-rows"),
    pytest.param(120_001, None, id="default-chunk"),
    pytest.param(2_200_000, 1_500_000, id="over-writer-cap"),
]


@pytest.mark.parametrize(("rows", "chunk"), CASES)
def test_stream_bypasses_threshold_and_keeps_base_row_groups(
    tmp_path: Path, rows: int, chunk: int | None
) -> None:
    table = rs.two_column_table(rows)
    cfg, lazy = rs.single_table_job(tmp_path, table)
    sink, target = rs.real_sink(tmp_path)
    knobs = {} if chunk is None else {"chunk_size_rows": chunk}
    result = rs.run_stream(cfg, {support.TABLE: lazy}, sink, **knobs)

    assert result.outputs == {}
    block = result.quality_metrics["auto_chunk"]
    assert block["input"]["mode"] == ("lazy" if rows else "resident")
    assert block["output"]["mode"] == "streamed"
    out = target / "t.parquet"
    got = pq.read_table(out)
    if rows == 0:
        # base `--chunked` writes the source schema; the resident run types the empty
        # columns differently, so the empty file is compared with the source, not the resident run.
        assert got.schema.equals(table.schema)
    else:
        assert got.equals(rs.reference(cfg, table).cast(got.schema))
    size = chunk or 50_000
    assert rs.row_groups(out) == (rs.expected_groups(rows, size) or [])
    assert out.read_bytes() == rs.per_chunk_file(got, size, tmp_path / "base.parquet")


def test_existing_mode_sites_see_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = _pipeline_context.decide_route

    def spy(ctx: Any) -> Any:
        seen.append(ctx)
        return real(ctx)

    monkeypatch.setattr(_pipeline_context, "decide_route", spy)
    cfg, lazy = rs.single_table_job(tmp_path, rs.two_column_table(10))
    sink, _ = rs.real_sink(tmp_path)
    rs.run_stream(cfg, {support.TABLE: lazy}, sink)
    assert [(c.execution_mode, c.stream_required) for c in seen] == [("auto", True)]


def test_auto_is_unchanged_below_the_threshold(tmp_path: Path) -> None:
    """The same 10-row job under `auto` still stays resident: nothing streams, nothing refuses."""
    table = rs.two_column_table(10)
    cfg, lazy = rs.single_table_job(tmp_path, table)
    sink, target = rs.real_sink(tmp_path)
    result = run_pipeline(
        cfg, sources={support.TABLE: lazy}, sink=sink, engine_version=rs.ENGINE_VERSION
    )
    assert sink.count("write_batches") == 0
    assert result.outputs[support.TABLE].equals(rs.reference(cfg, table))
    assert not target.exists()


def _refusal(
    cfg: dict[str, Any], sources: dict[str, Any], sink: Any, **extra: Any
) -> ExecutionError:
    with pytest.raises(ExecutionError) as caught:
        rs.run_stream(cfg, sources, sink, **extra)
    assert caught.value.code == reasons.runtime.STREAMING_UNAVAILABLE
    return caught.value


class _Reads:
    """Counts every way the given lazy sources hand out data rows.

    `profile_source` reads a bounded sample of each config path through its own handles before
    any route is decided, so only the handles the caller passed are watched."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, watched: dict[str, LazySource]) -> None:
        self.calls: list[str] = []
        self._ids = {id(src) for src in watched.values()}
        for name in ("open_batches", "iter_batches", "to_table"):
            real = getattr(LazySource, name)
            monkeypatch.setattr(LazySource, name, self._wrap(name, real))

    def _wrap(self, name: str, real: Any) -> Any:
        def spy(src: Any, *args: Any, **kwargs: Any) -> Any:
            if id(src) in self._ids:
                self.calls.append(name)
            return real(src, *args, **kwargs)

        return spy


def test_refusal_precedes_any_data_row_and_names_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = pa.table({"r": pa.array([rs.CANARY] * 5), "p": pa.array(["x"] * 5)})
    cols = [{"name": "r", "strategy": "shuffle", "namespace": "ns_r"}, support.pass_col("p")]
    cfg, lazy = rs.single_table_job(tmp_path, table, cols)
    sink, target = rs.real_sink(tmp_path)
    reads = _Reads(monkeypatch, {support.TABLE: lazy})
    error = _refusal(cfg, {support.TABLE: lazy}, sink)
    assert "strategy_not_chunk_safe" in error.message
    assert reads.calls == []
    assert rs.CANARY not in str(error)
    assert sink.calls == [] and not target.exists()


@pytest.mark.parametrize(
    "case",
    ["no-sink", "resident-source", "auto-chunk-off", "stream-output-off", "legacy-lane"],
)
def test_streaming_requirements_are_refused_not_degraded(tmp_path: Path, case: str) -> None:
    table = rs.two_column_table(10)
    cfg, lazy = rs.single_table_job(tmp_path, table)
    sink, _ = rs.real_sink(tmp_path)
    sources: dict[str, Any] = {support.TABLE: lazy}
    extra: dict[str, Any] = {}
    if case == "resident-source":
        sources = {support.TABLE: table}
    elif case == "auto-chunk-off":
        extra = {"auto_chunk": False}
    elif case == "stream-output-off":
        extra = {"stream_chunked_output": False}
    elif case == "legacy-lane":
        extra = {"chunked_dispatcher_enabled": False}
    with pytest.raises(ExecutionError) as caught:
        if case == "no-sink":
            run_pipeline(
                cfg,
                sources=sources,
                engine_version=rs.ENGINE_VERSION,
                execution_mode="stream",
            )
        else:
            rs.run_stream(cfg, sources, sink, **extra)
    assert caught.value.code == reasons.runtime.STREAMING_UNAVAILABLE


def _two_table_job(tmp_path: Path, rows: int = 12) -> tuple[dict[str, Any], dict[str, Any]]:
    from tests.unit.execution import _multi_table_support as mt

    cfg, resident = mt.build_job(
        tmp_path,
        {
            "a": (mt.std_columns("ns_a"), mt.string_table(rows, "a")),
            "b": (mt.std_columns("ns_b"), mt.string_table(rows, "b")),
        },
    )
    lazy = {n: LazySource(tmp_path / f"{n}.parquet") for n in resident}
    return cfg, {"lazy": lazy, "resident": resident}


def test_independent_multi_table_job_streams_every_table(tmp_path: Path) -> None:
    cfg, srcs = _two_table_job(tmp_path)
    sink, target = rs.real_sink(tmp_path)
    result = rs.run_stream(cfg, srcs["lazy"], sink, chunk_size_rows=5)
    tables = result.quality_metrics["auto_chunk"]["tables"]
    assert [(t["table"], t["dispatched"]) for t in tables] == [("a", True), ("b", True)]
    for name, source in srcs["resident"].items():
        got = pq.read_table(target / f"{name}.parquet")
        want = run_pipeline(
            {**cfg, "tables": [t for t in cfg["tables"] if t["name"] == name]},
            sources={name: source},
            engine_version=rs.ENGINE_VERSION,
            auto_chunk=False,
        ).outputs[name]
        assert got.equals(want.cast(got.schema))
        assert rs.row_groups(target / f"{name}.parquet") == rs.expected_groups(12, 5)


def test_multi_table_job_with_a_table_that_cannot_stream_is_refused(tmp_path: Path) -> None:
    cfg, srcs = _two_table_job(tmp_path)
    cfg["tables"][1]["columns"][0] = {"name": "h", "strategy": "shuffle", "namespace": "ns_b"}
    sink, _ = rs.real_sink(tmp_path)
    error = _refusal(cfg, srcs["lazy"], sink)
    assert "'b'" in error.message


# -- FK jobs ------------------------------------------------------------------------------------


def _fk_reference(cfg: dict[str, Any], tables: dict[str, pa.Table]) -> dict[str, pa.Table]:
    return dict(
        run_pipeline(
            cfg, sources=tables, engine_version=rs.ENGINE_VERSION, auto_chunk=False
        ).outputs
    )


def test_fk_tables_that_pass_the_complete_gate_run_as_lane_units(tmp_path: Path) -> None:
    cfg, lazy, tables = rs.fk_job(tmp_path)
    sink, target = rs.real_sink(tmp_path)
    result = rs.run_stream(cfg, lazy, sink, chunk_size_rows=7)
    assert result.outputs == {}
    entries = result.quality_metrics["auto_chunk"]["tables"]
    assert [(t["table"], t["dispatched"], t["output"]["mode"]) for t in entries] == [
        ("customers", True, "streamed"),
        ("orders", True, "streamed"),
    ]
    want = _fk_reference(cfg, tables)
    for name in tables:
        got = pq.read_table(target / f"{name}.parquet")
        assert got.equals(want[name].cast(got.schema)), name


def test_fk_split_is_not_applied_under_auto(tmp_path: Path) -> None:
    """M-E1a4: under `auto` an FK job never takes the per-table lane. A fidelity report keeps the
    job off the sequential route, so it reaches the B7 split decision, which still declines."""
    cfg, _lazy, tables = rs.fk_job(tmp_path)
    result = run_pipeline(
        cfg,
        sources=tables,
        engine_version=rs.ENGINE_VERSION,
        chunk_size_rows=7,
        auto_chunk_threshold_rows=1,
        fidelity_report=True,
    )
    assert "tables" not in result.quality_metrics.get("auto_chunk", {})
    assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"


def test_complete_gate_rejection_takes_guarded_out_of_core(tmp_path: Path) -> None:
    """A non-remap orphan policy fails the chunked gate (`chunked_fk_orphan_policy_not_remap`)
    on the child edge; out-of-core compatibility holds, the dtype hazard is clear."""
    cfg, lazy, tables = rs.fk_job(tmp_path, orphan_policy="preserve")
    sink, target = rs.real_sink(tmp_path)
    result = rs.run_stream(cfg, lazy, sink)
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    want = _fk_reference(cfg, tables)
    for name in tables:
        got = pq.read_table(target / f"{name}.parquet")
        assert got.equals(want[name].cast(got.schema)), name


def test_out_of_core_force_is_refused_on_a_dtype_hazard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline_routing_fk_dtype as dtype

    monkeypatch.setattr(dtype, "fk_out_of_core_dtype_hazard", lambda *a, **k: True)
    cfg, lazy, _ = rs.fk_job(tmp_path, orphan_policy="preserve")
    sink, target = rs.real_sink(tmp_path)
    reads = _Reads(monkeypatch, lazy)
    error = _refusal(cfg, lazy, sink)
    assert "fk_out_of_core_dtype_hazard" in error.message
    assert reads.calls == [] and not target.exists()


def test_fk_gate_failure_that_out_of_core_cannot_take_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent-side half of the complete gate: only the whole `check_chunked_compatibility`
    decides, so a failure it raises on a parent key is refused when out-of-core also declines."""
    from decoy_engine.execution import _pipeline_routing_signals as signals

    monkeypatch.setattr(
        signals, "out_of_core_admission", lambda *a, **k: (False, "unsupported_recipe")
    )
    cfg, lazy, _ = rs.fk_job(tmp_path, child_strategy="redact")
    sink, _target = rs.real_sink(tmp_path)
    reads = _Reads(monkeypatch, lazy)
    error = _refusal(cfg, lazy, sink)
    assert "chunked_fk" in error.message and "unsupported_recipe" in error.message
    assert reads.calls == []


@pytest.mark.parametrize("gate", ["reject_code_set_fk_keys", "reject_bucket_perturb_fk_keys"])
def test_parent_side_key_checks_belong_to_the_same_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    """M-E1a5: the FK split is admitted by `check_chunked_compatibility` as a whole. A failure of
    its parent-side `code_set` or `bucket_perturb` key check, which `gate_fk_child_edges` alone
    never sees, takes the job off the lane and onto the guarded out-of-core route."""
    from decoy_engine.execution import _chunked
    from decoy_engine.plan._errors import PlanCompileError

    module = (
        _chunked.code_set_gate
        if gate == "reject_code_set_fk_keys"
        else _chunked.bucket_perturb_gate
    )

    def reject(config: dict[str, Any], *, table: str) -> None:
        if table == "customers":
            raise PlanCompileError(
                code=reasons.plan.CHUNKED_CODE_SET_FK_KEY_UNSUPPORTED,
                path="x",
                message="parent key",
            )

    monkeypatch.setattr(module, gate, reject)
    cfg, lazy, tables = rs.fk_job(tmp_path)
    sink, target = rs.real_sink(tmp_path)
    result = rs.run_stream(cfg, lazy, sink)
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    want = _fk_reference(cfg, tables)
    for name in tables:
        got = pq.read_table(target / f"{name}.parquet")
        assert got.equals(want[name].cast(got.schema)), name


def test_forced_out_of_core_refusal_is_a_coded_streaming_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The out-of-core force fails closed: an error from the forced route becomes
    `streaming_unavailable`, with no context chained to it and nothing written."""
    from decoy_engine.execution import _pipeline_routing

    seen: list[str] = []

    def boom(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["execution_mode"])
        raise ExecutionError(code=reasons.route.OUT_OF_CORE_INELIGIBLE, message=rs.CANARY)

    monkeypatch.setattr(_pipeline_routing, "resolve_execution_route", boom)
    cfg, lazy, _ = rs.fk_job(tmp_path, orphan_policy="preserve")
    sink, target = rs.real_sink(tmp_path)
    error = _refusal(cfg, lazy, sink)
    assert seen == ["out_of_core"]
    assert error.__context__ is None and error.__cause__ is None
    assert rs.CANARY not in str(error)
    assert sink.calls == [] and not target.exists()


def test_declared_cast_route_decline_is_a_streaming_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared cast the chunked route cannot honour raises `DeclaredCastRouteError` from the
    complete gate. It is a compatibility decline like `PlanCompileError`: the guarded out-of-core
    evaluation still runs, and when that declines too the run is `streaming_unavailable`, not the
    raw route code, with an empty exception chain and no source read or sink write."""
    from decoy_engine.execution import _chunked
    from decoy_engine.execution import _pipeline_routing_signals as signals
    from decoy_engine.execution._errors import DeclaredCastRouteError

    def decline(config: dict[str, Any], *, table: str, **kwargs: Any) -> None:
        raise DeclaredCastRouteError(
            code=reasons.route.DECLARED_CAST_ROUTE_UNSUPPORTED, message=rs.CANARY
        )

    evaluated: list[bool] = []

    def out_of_core_declines(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        evaluated.append(True)
        return False, "unsupported_recipe"

    monkeypatch.setattr(_chunked, "check_chunked_compatibility", decline)
    monkeypatch.setattr(signals, "out_of_core_admission", out_of_core_declines)
    cfg, lazy, _ = rs.fk_job(tmp_path)
    sink, target = rs.real_sink(tmp_path)
    reads = _Reads(monkeypatch, lazy)
    error = _refusal(cfg, lazy, sink)
    assert evaluated == [True]
    assert reasons.route.DECLARED_CAST_ROUTE_UNSUPPORTED in error.message
    assert "unsupported_recipe" in error.message
    assert rs.CANARY not in str(error)
    assert error.__context__ is None and error.__cause__ is None
    assert reads.calls == []
    assert sink.calls == [] and not target.exists()


def test_execution_fails_closed_when_it_disagrees_with_the_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight and execution each decide the output mode; if execution would run resident
    after the preflight admitted streaming, the run is refused, never run resident."""
    from decoy_engine.execution import _chunked_output_sink as output_sink

    real = output_sink.decide_output_mode
    calls: list[int] = []

    def flaky(**kwargs: Any) -> tuple[str, str]:
        calls.append(1)
        return real(**kwargs) if len(calls) == 1 else ("resident", "no_sink")

    monkeypatch.setattr(output_sink, "decide_output_mode", flaky)
    cfg, lazy = rs.single_table_job(tmp_path, rs.two_column_table(10))
    sink, target = rs.real_sink(tmp_path)
    _refusal(cfg, {support.TABLE: lazy}, sink)
    assert len(calls) == 2
    assert sink.calls == [] and not target.exists()
