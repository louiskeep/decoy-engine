"""Acceptance tests 6 to 9 of plan 2026-10-02-b6a-incremental-output-sink (rev 2.1):
atomic publish, eligibility and the knob, result shape and evidence, the vault.

These tests are written before the implementation. Do not delete one, add a skip or
xfail outside `NEEDS_COMPANION`, or remove a commit/abort call-order assertion without
a new plan gate: a failing test is a defect in the code or a finding for the plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import ExecutionError, run_pipeline
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6a_support as b6a
from tests.unit.execution.test_auto_chunk_dispatcher import _row_error_job
from tests.unit.execution.test_isolated_worker_streaming import _fk_config

ROUTES = [pytest.param("native", marks=support.NEEDS_COMPANION), "oracle"]


def _job(tmp_path: Path, **extra_config: Any) -> tuple[dict[str, Any], pa.Table]:
    """Native without the companion for redact, truncate and passthrough; the oracle
    route is forced separately by removing the companion for hash."""
    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(support.ROWS)]),
            "r": pa.array([f"s{i}" for i in range(support.ROWS)]),
        }
    )
    path = support.write_source(src, tmp_path / "s.parquet")
    raw = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
        "tables": [{"name": "t", "columns": [support.hash_col("h"), support.redact_col("r")]}],
        "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
        **extra_config,
    }
    return PipelineConfig.model_validate(raw).model_dump(), src


def _native_job(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(
        {
            "r": pa.array([f"s{i}" for i in range(support.ROWS)]),
            "p": pa.array([f"keep-{i}" for i in range(support.ROWS)]),
        }
    )
    cfg = support.make_cfg(
        [support.redact_col("r"), support.pass_col("p")],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    return cfg, src


def _assert_clean(tmp_path: Path, target: Path) -> None:
    assert not target.exists()
    assert b6a.leftovers(tmp_path) == []


# ---------------------------------------------------------------------------
# Test 6: atomic publish.
# ---------------------------------------------------------------------------


def test_nothing_is_visible_until_the_single_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.native import _chunked_entry

    cfg, src = _native_job(tmp_path)
    sink, target = b6a.real_sink(tmp_path)
    seen: dict[str, Any] = {}
    real = _chunked_entry._mask_chunk_native
    calls = {"n": 0}

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            seen["target_exists"] = target.exists()
            seen["staging"] = b6a.leftovers(tmp_path)
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunked_entry, "_mask_chunk_native", spy)
    b6a.run_streamed(cfg, src, sink)
    assert seen["target_exists"] is False
    assert len(seen["staging"]) == 1 and seen["staging"][0].startswith("_decoy_stage_")
    assert sorted(p.name for p in target.iterdir()) == ["t.parquet"]
    assert b6a.leftovers(tmp_path) == []
    assert sink.calls == [("write_batches", "t"), ("commit",)]


def _kernel_failure(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    from decoy_engine.execution.native import _chunked_entry

    real = _chunked_entry._mask_chunk_native
    calls = {"n": 0}

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise exc
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunked_entry, "_mask_chunk_native", failing)


@pytest.mark.parametrize(
    "exc", [ExecutionError(code="injected", message="boom"), KeyboardInterrupt()], ids=str
)
def test_a_kernel_failure_aborts_once_and_publishes_nothing(
    exc: BaseException, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, src = _native_job(tmp_path)
    _kernel_failure(monkeypatch, exc)
    sink, target = b6a.real_sink(tmp_path)
    with pytest.raises(type(exc)) as err:
        b6a.run_streamed(cfg, src, sink)
    assert err.value is exc
    assert getattr(err.value, "code", None) == getattr(exc, "code", None)
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    _assert_clean(tmp_path, target)


@pytest.mark.parametrize("route", ROUTES)
def test_a_row_error_fails_closed_and_publishes_nothing(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if route == "oracle":
        support.remove_companion(monkeypatch)
    cfg, src = _row_error_job(tmp_path)
    sink, target = b6a.real_sink(tmp_path)
    with pytest.raises(RowErrorsFailedError):
        b6a.run_streamed(cfg, src, sink)
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    _assert_clean(tmp_path, target)


def test_an_abort_that_raises_does_not_mask_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, src = _native_job(tmp_path)
    exc = ExecutionError(code="injected", message="boom")
    _kernel_failure(monkeypatch, exc)
    sink = b6a.RecordingSink(abort_raises=True)
    with pytest.raises(ExecutionError) as err:
        b6a.run_streamed(cfg, src, sink)
    assert err.value is exc
    assert sink.count("abort") == 1


def test_a_non_empty_target_fails_the_commit_and_leaves_it_unchanged(tmp_path: Path) -> None:
    cfg, src = _native_job(tmp_path)
    sink, target = b6a.real_sink(tmp_path)
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    with pytest.raises(OSError):
        b6a.run_streamed(cfg, src, sink)
    assert [p.name for p in target.iterdir()] == ["keep.txt"]
    assert (target / "keep.txt").read_text() == "keep"
    assert b6a.leftovers(tmp_path) == []
    assert sink.calls == [("write_batches", "t"), ("commit",), ("abort",)]


def test_abort_is_never_called_after_a_successful_commit(tmp_path: Path) -> None:
    cfg, src = _native_job(tmp_path)
    sink, _target = b6a.real_sink(tmp_path)
    b6a.run_streamed(cfg, src, sink)
    assert sink.calls == [("write_batches", "t"), ("commit",)]


@pytest.mark.parametrize("stage", ["stamp_execution_metrics", "execution_telemetry"])
def test_a_failure_after_the_last_batch_and_before_commit_aborts_once(
    stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline_finalize
    from decoy_engine.execution import _pipeline_route_exec as route_exec

    cfg, src = _native_job(tmp_path)
    sentinel = RuntimeError(f"sentinel from {stage}")
    sink, target = b6a.real_sink(tmp_path)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        assert sink.calls and sink.calls[-1] == ("write_batches", "t")
        raise sentinel

    owner = _pipeline_finalize if stage == "stamp_execution_metrics" else route_exec
    monkeypatch.setattr(owner, stage, boom)
    with pytest.raises(RuntimeError) as err:
        b6a.run_streamed(cfg, src, sink)
    assert err.value is sentinel
    assert sink.count("abort") == 1 and sink.count("commit") == 0
    _assert_clean(tmp_path, target)


# ---------------------------------------------------------------------------
# Test 7: eligibility and the knob.
# ---------------------------------------------------------------------------


def _reason(result: Any) -> tuple[str, str | None]:
    block = result.quality_metrics["auto_chunk"]["output"]
    return block["mode"], block["reason"]


def _resident_case(name: str, tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """(config, run kwargs, sink) for each Design 2 reason."""
    extra: dict[str, Any] = {}
    kwargs: dict[str, Any] = {}
    sink: Any = b6a.RecordingSink()
    if name == "validators_present":
        extra["validators"] = [
            {"name": "regex_match", "columns": {"t": ["h"]}, "params": {"pattern": ".+"}}
        ]
    elif name == "quarantine_enabled":
        extra["quarantine"] = {"enabled": True, "output_path": str(tmp_path / "q.parquet")}
    elif name == "fidelity_report":
        kwargs["fidelity_report"] = True
    elif name == "post_validation":
        kwargs["post_validation"] = True
    elif name == "streaming_disabled":
        kwargs["stream_chunked_output"] = False
    elif name == "no_sink":
        sink = None
    elif name == "sink_not_streaming":
        sink = lambda _table, _data: None
    elif name == "legacy_lane":
        kwargs["chunked_dispatcher_enabled"] = False
    cfg, _src = _job(tmp_path, **extra)
    return cfg, kwargs, sink


@pytest.mark.parametrize(
    "reason",
    [
        "streaming_disabled",
        "no_sink",
        "sink_not_streaming",
        "legacy_lane",
        "validators_present",
        "quarantine_enabled",
        "fidelity_report",
        "post_validation",
    ],
)
def test_each_eligibility_reason_keeps_the_run_resident_and_the_sink_untouched(
    reason: str, tmp_path: Path
) -> None:
    cfg, kwargs, sink = _resident_case(reason, tmp_path)
    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(support.ROWS)]),
            "r": pa.array([f"s{i}" for i in range(support.ROWS)]),
        }
    )
    result = b6a.run_streamed(cfg, src, sink, **kwargs)
    assert _reason(result) == ("resident", reason)
    assert set(result.outputs) == {"t"}
    assert result.quality_metrics["execution"]["outputs_streamed"] is False
    if isinstance(sink, b6a.RecordingSink):
        assert sink.calls == []


def test_an_eligible_run_records_streamed_with_a_reason(tmp_path: Path) -> None:
    cfg, src = _job(tmp_path)
    result = b6a.run_streamed(cfg, src, b6a.RecordingSink())
    mode, reason = _reason(result)
    assert mode == "streamed" and isinstance(reason, str) and reason


@pytest.mark.parametrize("value", [None, 0, "yes"])
def test_an_invalid_knob_fails_before_profiling(
    value: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import decoy_engine.profile as profile_pkg

    def poison(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("profile_source ran before the knob was validated")

    monkeypatch.setattr(profile_pkg, "profile_source", poison)
    cfg, src = _job(tmp_path)
    with pytest.raises(ExecutionError) as err:
        b6a.run_streamed(cfg, src, b6a.RecordingSink(), stream_chunked_output=value)
    assert err.value.code == "invalid_execution_knob"


def _strip_ms(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_ms(v) for k, v in value.items() if not str(k).endswith("_ms")}
    if isinstance(value, (list, tuple)):
        return [_strip_ms(v) for v in value]
    return value


def _fk_run(tmp_path: Path, mode: str, sink: Any, **extra: Any) -> Any:
    config, manifest = _fk_config(tmp_path)
    sources = {name: pq.read_table(path) for name, path in manifest.items()}
    return run_pipeline(
        config,
        sources,
        engine_version="b6a-test",
        execution_mode=mode,
        sink=sink,
        **extra,
    )


@pytest.mark.parametrize(
    "case",
    ["unified_slice_default", "plain_full_frame", "auto_chunk_off", "sequential", "out_of_core"],
)
def test_non_routed_runs_never_enter_the_new_code(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = b6a.sink_module()
    entered: list[Any] = []
    real = mod.decide_output_mode
    monkeypatch.setattr(
        mod, "decide_output_mode", lambda *a, **k: (entered.append(1), real(*a, **k))[1]
    )
    results = {}
    sinks = {}
    for label, extra in (("new", {}), ("off", b6a.resident_kw())):
        sink = b6a.RecordingSink()
        sinks[label] = sink
        run_dir = tmp_path / label
        run_dir.mkdir()
        if case in ("sequential", "out_of_core"):
            results[label] = _fk_run(run_dir, case, sink, **extra)
            continue
        cfg, src = _job(run_dir)
        kwargs: dict[str, Any] = {"auto_chunk_threshold_rows": 10_000}
        if case == "plain_full_frame":
            kwargs["unified_slice_enabled"] = False
        if case == "auto_chunk_off":
            kwargs["auto_chunk"] = False
            kwargs["auto_chunk_threshold_rows"] = 10
        results[label] = b6a.run_streamed(cfg, src, sink, **{**kwargs, **extra})
    assert entered == []
    new, off = results["new"], results["off"]
    assert sinks["new"].calls == sinks["off"].calls
    assert list(new.outputs) == list(off.outputs)
    for name in new.outputs:
        assert new.outputs[name].equals(off.outputs[name], check_metadata=True)
    assert _strip_ms(new.quality_metrics) == _strip_ms(off.quality_metrics)
    assert "output" not in new.quality_metrics.get("auto_chunk", {})


# ---------------------------------------------------------------------------
# Test 8: result shape and evidence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_streamed_result_shape_and_evidence(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if route == "oracle":
        support.remove_companion(monkeypatch)
    cfg, src = _job(tmp_path)
    resident = b6a.run_streamed(cfg, src, None, **b6a.resident_kw())
    first = b6a.run_streamed(cfg, src, b6a.RecordingSink())
    second = b6a.run_streamed(cfg, src, b6a.RecordingSink())
    assert first.outputs == {} and first.row_errors == ()
    assert first.table_kinds == resident.table_kinds
    execution = dict(first.quality_metrics["execution"])
    assert execution.pop("outputs_streamed") is True
    expected = dict(resident.quality_metrics["execution"])
    assert expected.pop("outputs_streamed") is False
    assert execution == expected
    block = first.quality_metrics["auto_chunk"]["output"]
    assert block["mode"] == "streamed"
    for key in ("row_groups", "byte_cut_row_groups", "held_back_chunks", "spilled_chunks"):
        assert isinstance(block[key], int), key
    assert block["row_groups"] == 1 and block["byte_cut_row_groups"] == 0
    json.dumps(first.quality_metrics["auto_chunk"], allow_nan=False)
    assert first.quality_metrics == second.quality_metrics
    resident_block = resident.quality_metrics["auto_chunk"]["output"]
    assert resident_block["mode"] == "resident"
    assert {k: v for k, v in first.quality_metrics["auto_chunk"].items() if k != "output"} == {
        k: v for k, v in resident.quality_metrics["auto_chunk"].items() if k != "output"
    }


# ---------------------------------------------------------------------------
# Test 9: vault.
# ---------------------------------------------------------------------------


def _vault_job(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(support.ROWS)]),
            "r": pa.array([f"s{i}" for i in range(support.ROWS)]),
        }
    )
    cfg = support.make_cfg(
        [
            {**support.hash_col("h"), "vault": True},
            {**support.redact_col("r"), "vault": True, "namespace": "r_ns"},
        ],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    return cfg, src


@pytest.mark.parametrize("route", ROUTES)
def test_streamed_vault_entries_and_file_equal_the_resident_run(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("cryptography")
    from decoy_engine.plan._seed import _normalize_job_seed
    from decoy_engine.vault import load_vault, vault_writer_for_config

    if route == "oracle":
        support.remove_companion(monkeypatch)
    cfg, src = _vault_job(tmp_path)
    streamed_writer, resident_writer = vault_writer_for_config(cfg), vault_writer_for_config(cfg)
    sink = b6a.RecordingSink()
    b6a.run_streamed(cfg, src, sink, vault_writer=streamed_writer)
    assert sink.count("commit") == 1
    b6a.run_streamed(cfg, src, None, vault_writer=resident_writer, **b6a.resident_kw())
    assert streamed_writer._entries == resident_writer._entries
    assert len(streamed_writer._entries) == 2 * support.ROWS
    maps = {}
    for label, writer in (("streamed", streamed_writer), ("resident", resident_writer)):
        path = tmp_path / f"{label}.vault"
        writer.write(path)
        maps[label], _ambiguous = load_vault(path, _normalize_job_seed(cfg))
    assert maps["streamed"] == maps["resident"]


def test_a_vault_writer_keyed_differently_is_rejected_before_anything_is_staged(
    tmp_path: Path,
) -> None:
    from decoy_engine.keyprovider import SecretKeyProvider
    from decoy_engine.vault import VaultError, VaultWriter

    cfg, src = _vault_job(tmp_path)
    sink, target = b6a.real_sink(tmp_path)
    with pytest.raises((VaultError, ExecutionError)):
        b6a.run_streamed(
            cfg,
            src,
            sink,
            vault_writer=VaultWriter((42).to_bytes(8, "big")),
            key_provider=SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
        )
    assert sink.calls == []
    _assert_clean(tmp_path, target)
