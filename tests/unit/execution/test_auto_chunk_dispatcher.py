"""Acceptance tests 1, 2, 4 to 11 of plan 2026-10-01-dispatcher-auto-chunk (rev 5):
auto-chunk runs on the chunked dispatcher (B1's `run_mask_chunked`).

Test 0 (output-delta record) is `test_auto_chunk_output_delta`; tests 3, 3a, 3b
(output contract) are `test_auto_chunk_output_contract`; test 12 (sentries) and
13 (benchmark) live with their own harnesses. These tests are written before the
implementation. Do not delete one, add a skip or xfail outside `NEEDS_COMPANION`,
or remove a lane, `lane_reason` or backend-evidence assertion without a new plan
gate: a failing test is a defect in the code or a finding for the plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import ExecutionError, run_pipeline
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution.test_auto_chunk_routing import (
    _CHUNK,
    _LOW_THRESHOLD,
    _STRATEGY_MATRIX,
    _single_column_job,
)
from tests.unit.execution.test_auto_chunk_routing import _ENGINE_VERSION as ROUTING_VERSION

SIX_KEYS = ("mode", "chunk_size_rows", "threshold_rows", "source_rows", "chunk_count", "reason")


def _job(
    tmp_path: Path, columns: list[dict[str, Any]], data: dict[str, pa.Array] | None = None
) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(data or support.string_source())
    return support.make_cfg(columns, path=support.write_source(src, tmp_path / "s.parquet")), src


def _hash_redact_job(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    return _job(tmp_path, [support.hash_col("h"), support.redact_col("r")])


def _native_job(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    """Redact, truncate and passthrough: native on B1 with no companion at all."""
    return _job(
        tmp_path,
        [support.redact_col("r"), support.truncate_col("z"), support.pass_col("p")],
        {
            "r": pa.array([f"s{i}" for i in range(support.ROWS)]),
            "z": pa.array([f"{i:05d}" for i in range(support.ROWS)]),
            "p": pa.array([f"keep-{i}" for i in range(support.ROWS)]),
        },
    )


# ---------------------------------------------------------------------------
# Test 1: when is unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec_name", sorted(_STRATEGY_MATRIX))
def test_when_is_unchanged_across_the_routing_matrix(
    spec_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    cfg, sources = _single_column_job(tmp_path, spec_name)
    kwargs = {
        "engine_version": ROUTING_VERSION,
        "auto_chunk_threshold_rows": _LOW_THRESHOLD,
        "chunk_size_rows": _CHUNK,
        "explain_plan": True,
    }
    on = run_pipeline(cfg, sources=sources, chunked_dispatcher_enabled=True, **kwargs)
    off = run_pipeline(cfg, sources=sources, chunked_dispatcher_enabled=False, **kwargs)
    assert on.quality_metrics["auto_chunk"]["mode"] == "chunked"
    for key in SIX_KEYS:
        assert on.quality_metrics["auto_chunk"][key] == off.quality_metrics["auto_chunk"][key], key
    assert on.quality_metrics["execution_plan"] == off.quality_metrics["execution_plan"]
    assert on.outputs["accounts"].column("val").to_pylist() == (
        off.outputs["accounts"].column("val").to_pylist()
    )


def _non_routed_runs(tmp_path: Path) -> dict[str, tuple[dict[str, Any], dict[str, pa.Table], dict]]:
    """name -> (config, sources, run kwargs) for jobs that never route chunked."""
    cfg, sources = _single_column_job(tmp_path, "hash")
    base = {"engine_version": ROUTING_VERSION}
    df = pd.DataFrame({"val": pd.array([1, None, 3, 4] * 15, dtype="Int64").astype(float)})
    cases: dict[str, tuple[dict[str, Any], dict[str, pa.Table], dict]] = {
        "below_threshold_default_knobs": (cfg, sources, dict(base)),
        "auto_chunk_off": (
            cfg,
            sources,
            {**base, "auto_chunk": False, "auto_chunk_threshold_rows": _LOW_THRESHOLD},
        ),
    }
    bucket_cfg = _single_column_job(tmp_path, "bucketize")[0]
    cases["int_with_nulls_stays_full_frame"] = (
        bucket_cfg,
        {"accounts": pa.Table.from_pandas(df.astype("Int64"), preserve_index=False)},
        {**base, "auto_chunk_threshold_rows": _LOW_THRESHOLD, "chunk_size_rows": _CHUNK},
    )
    return cases


def test_non_routed_jobs_have_equal_quality_metrics_for_any_valid_knobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    for name, (cfg, sources, kwargs) in _non_routed_runs(tmp_path).items():
        baseline = run_pipeline(cfg, sources=sources, **kwargs)
        assert baseline.quality_metrics.get("auto_chunk", {}).get("mode") != "chunked", name
        for knobs in (
            {"native_threads": 4},
            {"chunked_dispatcher_enabled": False},
            {"native_threads": 1024, "chunked_dispatcher_enabled": True},
        ):
            other = run_pipeline(cfg, sources=sources, **kwargs, **knobs)
            assert other.quality_metrics == baseline.quality_metrics, (name, knobs)
            for table in baseline.outputs:
                assert other.outputs[table].equals(baseline.outputs[table], check_metadata=True)


def test_the_all_default_small_run_stamps_nothing_chunk_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing golden (`test_below_threshold_default_run_stamps_nothing`) still
    holds with the two new knobs at their defaults and when passed explicitly."""
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    cfg, sources = _single_column_job(tmp_path, "hash")
    for extra in ({}, {"native_threads": 1, "chunked_dispatcher_enabled": True}):
        result = run_pipeline(cfg, sources=sources, engine_version=ROUTING_VERSION, **extra)
        assert set(result.quality_metrics) - {"unified_slice_activation"} == {"execution"}


# ---------------------------------------------------------------------------
# Test 2: lane.
# ---------------------------------------------------------------------------


def test_routed_job_calls_run_mask_chunked_once_with_the_run_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.keyprovider import SecretKeyProvider
    from decoy_engine.vault import vault_writer_for_config

    cfg, src = _job(
        tmp_path,
        [{**support.redact_col("r"), "vault": True, "namespace": "r_ns"}, support.hash_col("h")],
    )
    provider = SecretKeyProvider(secret=bytes(range(32)), key_version="v1")
    writer = vault_writer_for_config(cfg, key_provider=provider)
    spies = support.spy_lanes(monkeypatch)
    result = support.run_default(
        cfg, src, native_threads=3, vault_writer=writer, key_provider=provider
    )
    calls = spies["entry.run_mask_chunked"]
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] is cfg or args[0] == cfg
    assert kwargs["table"] == support.TABLE
    assert type(kwargs["adapter"]) is PandasExecutionAdapter
    assert kwargs["registry"] is not None
    assert kwargs["vault_writer"] is writer
    assert kwargs["key_provider"] is provider
    assert kwargs["native_threads"] == 3
    assert spies["oracle.run_mask_pipeline_chunked"] == []
    block = result.quality_metrics["auto_chunk"]
    assert block["lane"] == "dispatcher"
    assert block["lane_reason"] is None
    assert block["native_threads"] == 3
    assert block["mode"] == "chunked"


def test_kill_switch_runs_todays_lane_and_records_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, src = _hash_redact_job(tmp_path)
    spies = support.spy_lanes(monkeypatch)
    result = run_pipeline(
        cfg, sources={support.TABLE: src}, **support.run_kwargs(chunked_dispatcher_enabled=False)
    )
    assert spies["entry.run_mask_chunked"] == []
    assert len(spies["oracle.run_mask_pipeline_chunked"]) == 1
    block = result.quality_metrics["auto_chunk"]
    assert block["lane"] == "legacy_oracle"
    assert block["lane_reason"] == "dispatcher_disabled"
    for key in SIX_KEYS:
        assert key in block


def _formerly_legacy_sources(tmp_path: Path) -> dict[str, tuple[pa.Table, list[dict[str, Any]]]]:
    n = support.ROWS
    base = support.string_source()
    both = [support.hash_col("h"), support.redact_col("r")]
    cases: dict[str, tuple[pa.Table, list[dict[str, Any]]]] = {}
    cases["date64_passthrough"] = (
        support.table_of({**base, "x": pa.array([i * 86_400_000 for i in range(n)], pa.date64())}),
        [*both, support.pass_col("x")],
    )
    cases["time64_ns_aligned"] = (
        support.table_of({**base, "x": pa.array([i * 1000 for i in range(n)], pa.time64("ns"))}),
        [*both, support.pass_col("x")],
    )
    df = pd.DataFrame({"h": list(base["h"].to_pylist()), "r": list(base["r"].to_pylist())}).astype(
        {"h": "string"}
    )
    cases["pandas_metadata_StringDtype"] = (pa.Table.from_pandas(df, preserve_index=False), both)
    df2 = pd.DataFrame({"h": base["h"].to_pylist(), "r": base["r"].to_pylist()})
    df2.attrs = {"bench": "b2"}
    cases["DataFrame_attrs"] = (pa.Table.from_pandas(df2, preserve_index=False), both)
    written = tmp_path / "pandas_written.parquet"
    pq.write_table(pa.Table.from_pandas(df2, preserve_index=False), written)
    cases["pandas_written_parquet_read_back"] = (pq.read_table(written), both)
    return cases


def test_sources_revision_3_1_sent_to_the_legacy_lane_take_the_dispatcher_lane(
    tmp_path: Path,
) -> None:
    for name, (src, columns) in _formerly_legacy_sources(tmp_path).items():
        cfg = support.make_cfg(
            columns, path=support.write_source(src, tmp_path / f"{name}.parquet")
        )
        block = support.run_default(cfg, src).quality_metrics["auto_chunk"]
        assert block["mode"] == "chunked", name
        assert block["lane"] == "dispatcher", name
        assert block["lane_reason"] is None, name


# ---------------------------------------------------------------------------
# Test 4: native refusals are surfaced, with output satisfying guarantee 3.
# ---------------------------------------------------------------------------


def _backends(evidence: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {c["column"]: (c["planned_backend"], c["executed_backend"]) for c in evidence["columns"]}


_TYPED_SOURCES: dict[str, pa.Array] = {
    "float_whole": pa.array([float(i) for i in range(support.ROWS)], pa.float64()),
    "float32_whole": pa.array([float(i) for i in range(support.ROWS)], pa.float32()),
    "int8": pa.array([i % 100 for i in range(support.ROWS)], pa.int8()),
    "large_string": pa.array([f"k{i}" for i in range(support.ROWS)], pa.large_string()),
    "time32": pa.array(list(range(support.ROWS)), pa.time32("s")),
    "duration": pa.array(list(range(support.ROWS)), pa.duration("s")),
    "date64": pa.array([i * 86_400_000 for i in range(support.ROWS)], pa.date64()),
}


@pytest.mark.parametrize("strategy", ["hash", "truncate", "redact"])
@pytest.mark.parametrize("typ", sorted(_TYPED_SOURCES))
def test_planned_backend_is_the_same_on_both_lanes(typ: str, strategy: str, tmp_path: Path) -> None:
    # The kill-switch lane profiles the real first chunk, as the dispatcher lane does,
    # so a type-dependent admission decision reports the same planned backend.
    col = {
        "hash": support.hash_col,
        "truncate": support.truncate_col,
        "redact": support.redact_col,
    }[strategy]("x")
    data = {**support.string_source(), "x": _TYPED_SOURCES[typ]}
    cfg, src = _job(tmp_path, [support.hash_col("h"), support.redact_col("r"), col], data)
    planned: dict[str, Any] = {}
    for lane, run in (("dispatcher", support.run_default), ("legacy", support.run_legacy)):
        try:
            columns = run(cfg, src).quality_metrics["chunked_route"]["columns"]
        except Exception as exc:  # a type both lanes refuse must be refused identically
            planned[lane] = type(exc).__name__
            continue
        planned[lane] = {c["column"]: c["planned_backend"] for c in columns}
    assert planned["legacy"] == planned["dispatcher"]


@pytest.mark.parametrize("spec_name", sorted(_STRATEGY_MATRIX))
def test_planned_backend_matches_across_lanes_for_the_strategy_matrix(
    spec_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    cfg, sources = _single_column_job(tmp_path, spec_name)
    planned = {}
    for lane, extra in (("dispatcher", {}), ("legacy", {"chunked_dispatcher_enabled": False})):
        result = run_pipeline(
            cfg,
            sources=sources,
            engine_version=ROUTING_VERSION,
            auto_chunk_threshold_rows=_LOW_THRESHOLD,
            chunk_size_rows=_CHUNK,
            **extra,
        )
        columns = result.quality_metrics["chunked_route"]["columns"]
        planned[lane] = {c["column"]: c["planned_backend"] for c in columns}
    assert planned["legacy"] == planned["dispatcher"]


def test_mixed_table_with_a_categorical_column_runs_wholly_on_the_oracle_route(
    tmp_path: Path,
) -> None:
    data = {
        "h": support.string_source()["h"],
        "c": pa.array([["a", "b", "c"][i % 3] for i in range(support.ROWS)]),
    }
    cat = {
        "name": "c",
        "strategy": "categorical",
        "deterministic": True,
        "namespace": "ns_c",
        "provider_config": {"categories": ["a", "b", "c"]},
    }
    cfg, src = _job(tmp_path, [support.hash_col("h"), cat], data)
    result = support.run_default(cfg, src)
    evidence = result.quality_metrics["chunked_route"]
    assert evidence["native_admitted"] is False
    assert evidence["reroute_reason"] == "categorical_not_native_chunked_route:c"
    backends = _backends(evidence)
    assert backends["c"] == ("pandas_oracle", "pandas_oracle")
    assert backends["h"][0] == "rust_companion" and backends["h"][1] == "pandas_oracle"
    legacy = support.run_legacy(cfg, src)
    support.check_contract(
        result.outputs[support.TABLE],
        legacy.outputs[support.TABLE],
        None,
        src,
        string_output={"h"},
        masked={"c"},
    )


def test_unconfigured_column_reroutes_with_uncovered_columns(tmp_path: Path) -> None:
    data = {**support.string_source(), "extra": pa.array([f"e{i}" for i in range(support.ROWS)])}
    cfg, src = _job(tmp_path, [support.hash_col("h"), support.redact_col("r")], data)
    result = support.run_default(cfg, src)
    evidence = result.quality_metrics["chunked_route"]
    assert evidence["native_admitted"] is False
    assert evidence["reroute_reason"].startswith("uncovered_columns")
    assert all(executed == "pandas_oracle" for _planned, executed in _backends(evidence).values())
    legacy = support.run_legacy(cfg, src)
    support.check_contract(
        result.outputs[support.TABLE],
        legacy.outputs[support.TABLE],
        None,
        src,
        string_output={"h", "r"},
    )


def test_hash_with_the_crypto_kernel_unavailable_is_planned_rust_and_executed_on_pandas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support.remove_companion(monkeypatch)
    cfg, src = _hash_redact_job(tmp_path)
    result = support.run_default(cfg, src)
    evidence = result.quality_metrics["chunked_route"]
    assert evidence["native_admitted"] is False
    assert evidence["reroute_reason"] == "crypto_extension_unavailable"
    assert _backends(evidence)["h"] == ("rust_companion", "pandas_oracle")
    legacy = support.run_legacy(cfg, src)
    support.check_contract(
        result.outputs[support.TABLE],
        legacy.outputs[support.TABLE],
        None,
        src,
        string_output={"h", "r"},
    )


def test_faker_with_the_index_kernel_unavailable_is_planned_pool_select_and_executed_on_pandas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.execution import _auto_chunk_strategies as strategies

    support.remove_companion(monkeypatch)
    columns, data = strategies.STRATEGY_FIXTURES["faker:deterministic_native"]
    cfg, src = _job(tmp_path, columns, data)
    evidence = support.run_default(cfg, src).quality_metrics["chunked_route"]
    assert evidence["native_admitted"] is False
    assert evidence["reroute_reason"] == "index_extension_unavailable"
    assert _backends(evidence)["val"] == ("rust_pool_select", "pandas_oracle")


def test_redact_truncate_passthrough_with_no_companion_run_natively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support.remove_companion(monkeypatch)
    cfg, src = _native_job(tmp_path)
    result = support.run_default(cfg, src)
    evidence = result.quality_metrics["chunked_route"]
    assert evidence["native_admitted"] is True
    assert evidence["reroute_reason"] is None
    assert set(_backends(evidence).values()) == {("arrow_python", "arrow_python")}
    legacy = support.run_legacy(cfg, src)
    support.check_contract(
        result.outputs[support.TABLE],
        legacy.outputs[support.TABLE],
        None,
        src,
        string_output={"r", "z"},
    )


# ---------------------------------------------------------------------------
# Test 5: evidence shape.
# ---------------------------------------------------------------------------


def _no_elapsed(node: Any) -> None:
    if isinstance(node, dict):
        assert "elapsed_ms" not in node
        for value in node.values():
            _no_elapsed(value)
    elif isinstance(node, list):
        for value in node:
            _no_elapsed(value)


@pytest.mark.parametrize("lane", ["dispatcher", "legacy"])
def test_chunked_route_evidence_is_json_safe_deterministic_and_complete(
    lane: str, tmp_path: Path
) -> None:
    data = {**support.string_source(), "extra": pa.array([f"e{i}" for i in range(support.ROWS)])}
    cfg, src = _job(tmp_path, [support.hash_col("h"), support.redact_col("r")], data)
    run = support.run_default if lane == "dispatcher" else support.run_legacy
    first, second = run(cfg, src), run(cfg, src)
    evidence = first.quality_metrics["chunked_route"]
    assert json.loads(json.dumps(evidence, allow_nan=False)) == evidence
    _no_elapsed(evidence)
    chunk_count = first.quality_metrics["auto_chunk"]["chunk_count"]
    assert all(c["calls"] == chunk_count for c in evidence["columns"])
    # `extra` is an unconfigured passthrough column: read by pandas on both lanes here
    # (the table reroutes to B1's oracle route), listed sorted.
    assert evidence["pandas_read_passthrough"] == sorted(evidence["pandas_read_passthrough"])
    assert first.quality_metrics == second.quality_metrics
    if lane == "legacy":
        assert evidence["native_admitted"] is False
        assert evidence["reroute_reason"] == "dispatcher_disabled"
        assert evidence["pandas_read_passthrough"] == ["extra"]
        assert all(executed == "pandas_oracle" for _p, executed in _backends(evidence).values())
        assert _backends(evidence)["h"][0] == "rust_companion"


def test_legacy_lane_evidence_lists_every_configured_passthrough_column(tmp_path: Path) -> None:
    cfg, src = _native_job(tmp_path)
    evidence = support.run_legacy(cfg, src).quality_metrics["chunked_route"]
    assert evidence["pandas_read_passthrough"] == ["p"]


def test_timings_hold_one_record_per_strategy_and_column_summed_over_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, src = _native_job(tmp_path)
    spies = support.spy_lanes(monkeypatch)
    result = support.run_default(cfg, src)
    ((_args, kwargs),) = spies["entry.run_mask_chunked"]
    sums: dict[tuple[str, str], float] = {}
    for chunk_result in kwargs["chunk_result_sink"]:
        for rec in chunk_result.timings:
            key = (rec.strategy_type, rec.column)
            sums[key] = sums.get(key, 0.0) + rec.elapsed_ms
    assert len(kwargs["chunk_result_sink"]) == result.quality_metrics["auto_chunk"]["chunk_count"]
    keys = [(t.strategy_type, t.column) for t in result.timings]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(sums)
    for t in result.timings:
        assert t.elapsed_ms == pytest.approx(sums[(t.strategy_type, t.column)])
    assert result.boundary_conversion_ms == 0.0


# ---------------------------------------------------------------------------
# Test 6: vault.
# ---------------------------------------------------------------------------


def _vault_job(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    return _job(
        tmp_path,
        [
            {**support.hash_col("h"), "vault": True},
            {**support.redact_col("r"), "vault": True, "namespace": "r_ns"},
        ],
    )


@pytest.mark.parametrize("route", ["native", "oracle"])
def test_vault_entries_equal_the_dispatcher_off_run(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.vault import vault_writer_for_config

    if route == "oracle":
        support.remove_companion(monkeypatch)
    elif not support.COMPANION_PRESENT:
        pytest.skip("compiled companion not installed")
    cfg, src = _vault_job(tmp_path)
    on, off, full = (vault_writer_for_config(cfg) for _ in range(3))
    support.run_default(cfg, src, vault_writer=on)
    support.run_legacy(cfg, src, vault_writer=off)
    support.run_full_frame(cfg, src, vault_writer=full)
    assert on._entries == off._entries == full._entries
    assert len(on._entries) == 2 * support.ROWS


def test_vault_file_round_trip_equals_the_dispatcher_off_run(tmp_path: Path) -> None:
    pytest.importorskip("cryptography")
    from decoy_engine.plan._seed import _normalize_job_seed
    from decoy_engine.vault import load_vault, vault_writer_for_config

    # Redact is lossy (every source maps to one masked value, so its entries are
    # ambiguous by design); the file round trip is checked on the hash column.
    cfg, src = _job(tmp_path, [{**support.hash_col("h"), "vault": True}, support.redact_col("r")])
    maps = {}
    for label, run in (("on", support.run_default), ("off", support.run_legacy)):
        writer = vault_writer_for_config(cfg)
        run(cfg, src, vault_writer=writer)
        path = tmp_path / f"{label}.vault"
        writer.write(path)
        maps[label], ambiguous = load_vault(path, _normalize_job_seed(cfg))
        assert ambiguous == 0
    assert maps["on"] == maps["off"]
    assert len(maps["on"]) == support.ROWS


def test_a_vault_writer_keyed_differently_from_the_mask_key_is_rejected_before_any_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.keyprovider import SecretKeyProvider
    from decoy_engine.vault import VaultError, VaultWriter

    cfg, src = _vault_job(tmp_path)
    spies = support.spy_lanes(monkeypatch)
    with pytest.raises((VaultError, ExecutionError)):
        support.run_default(
            cfg,
            src,
            vault_writer=VaultWriter((42).to_bytes(8, "big")),
            key_provider=SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
        )
    assert spies["entry.run_mask_chunked"] == []
    assert spies["oracle.run_mask_pipeline_chunked"] == []


# ---------------------------------------------------------------------------
# Test 7: row errors.
# ---------------------------------------------------------------------------


def _row_error_job(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    values = [f"2020-01-{1 + i % 28:02d}" for i in range(support.ROWS)]
    values[20] = "not-a-date"
    column = {
        "name": "val",
        "strategy": "date_shift",
        "namespace": "d",
        "provider_config": {"min_days": -3, "max_days": 3, "date_format": "%Y-%m-%d"},
    }
    return _job(tmp_path, [column], {"val": pa.array(values)})


@pytest.mark.parametrize("route", ["native", "oracle"])
def test_a_row_error_fails_closed_exactly_as_the_dispatcher_off_run(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if route == "oracle":
        support.remove_companion(monkeypatch)
    cfg, src = _row_error_job(tmp_path)
    spies = support.spy_lanes(monkeypatch)
    with pytest.raises(RowErrorsFailedError) as legacy:
        support.run_legacy(cfg, src)
    with pytest.raises(RowErrorsFailedError) as dispatcher:
        support.run_default(cfg, src)
    assert len(spies["entry.run_mask_chunked"]) == 1
    assert [r.trigger for r in dispatcher.value.records] == [
        r.trigger for r in legacy.value.records
    ]
    assert "not-a-date" not in str(dispatcher.value)


# ---------------------------------------------------------------------------
# Test 8: threads.
# ---------------------------------------------------------------------------


@pytest.fixture
def forbid_profiling(monkeypatch: pytest.MonkeyPatch) -> None:
    import decoy_engine.profile as profile_mod

    def bomb(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("profile_source ran before the native_threads check")

    monkeypatch.setattr(profile_mod, "profile_source", bomb)


@pytest.mark.parametrize("routed", [True, False])
@pytest.mark.parametrize("bad", [0, -1, 1025, True, "2"])
def test_invalid_native_threads_fail_early_on_every_call(
    bad: Any, routed: bool, tmp_path: Path, forbid_profiling: None
) -> None:
    cfg, src = _native_job(tmp_path)
    kwargs = support.run_kwargs(native_threads=bad)
    if not routed:
        kwargs["auto_chunk_threshold_rows"] = 10**9
    with pytest.raises(ExecutionError) as raised:
        run_pipeline(cfg, sources={support.TABLE: src}, **kwargs)
    assert raised.value.code == "invalid_execution_knob"
    assert "native_threads" in str(raised.value)


def test_the_upper_bound_is_b1s_constant_and_1024_is_accepted(tmp_path: Path) -> None:
    from decoy_engine.execution import _pipeline, _pipeline_auto_chunk
    from decoy_engine.execution.native._chunked_entry import MAX_NATIVE_THREADS

    assert MAX_NATIVE_THREADS == 1024
    for module in (_pipeline, _pipeline_auto_chunk):
        source = Path(module.__file__).read_text()
        assert "1024" not in source, f"{module.__name__} carries a second literal for the bound"
    cfg, src = _native_job(tmp_path)
    result = support.run_default(cfg, src, native_threads=MAX_NATIVE_THREADS)
    assert result.quality_metrics["auto_chunk"]["native_threads"] == MAX_NATIVE_THREADS
    with pytest.raises(ExecutionError) as raised:
        support.run_default(cfg, src, native_threads=MAX_NATIVE_THREADS + 1)
    assert raised.value.code == "invalid_execution_knob"


def test_native_threads_defaults_to_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, src = _native_job(tmp_path)
    spies = support.spy_lanes(monkeypatch)
    result = support.run_default(cfg, src)
    assert [kw["native_threads"] for _a, kw in spies["entry.run_mask_chunked"]] == [1]
    assert result.quality_metrics["auto_chunk"]["native_threads"] == 1


def test_native_threads_reaches_the_dispatcher_lane_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, src = _native_job(tmp_path)
    spies = support.spy_lanes(monkeypatch)
    support.run_default(cfg, src, native_threads=4)
    assert [kw["native_threads"] for _a, kw in spies["entry.run_mask_chunked"]] == [4]
    support.run_legacy(cfg, src, native_threads=4)
    assert len(spies["entry.run_mask_chunked"]) == 1


@support.NEEDS_COMPANION
def test_native_threads_reaches_the_compiled_hash_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.native import _kernels_keyed
    from decoy_engine.execution.native._crypto_ext import load_compiled_crypto_kernel

    real = load_compiled_crypto_kernel()
    seen: list[Any] = []

    class _Recording:
        def derive_batch(self, values: Any, *, native_threads: Any = None, **kw: Any) -> Any:
            seen.append(native_threads)
            return real.derive_batch(values, native_threads=native_threads, **kw)

    monkeypatch.setattr(_kernels_keyed, "load_compiled_crypto_kernel", lambda: _Recording())
    cfg, src = _hash_redact_job(tmp_path)
    one = support.run_default(cfg, src, native_threads=1)
    four = support.run_default(cfg, src, native_threads=4)
    assert set(seen) == {1, 4}
    assert one.outputs[support.TABLE].equals(four.outputs[support.TABLE], check_metadata=True)
    seen.clear()
    support.run_legacy(cfg, src, native_threads=4)
    assert seen == [], "today's lane must not reach the compiled kernels"


# ---------------------------------------------------------------------------
# Test 9: no mid-run fallback.
# ---------------------------------------------------------------------------


def test_a_native_failure_on_chunk_two_propagates_and_nothing_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _chunked_oracle
    from decoy_engine.execution.native import _chunked_entry
    from decoy_engine.vault import vault_writer_for_config

    cfg, src = _job(
        tmp_path,
        [{**support.redact_col("r"), "vault": True, "namespace": "r_ns"}],
        {"r": support.string_source()["r"]},
    )
    real = _chunked_entry._mask_chunk_native
    calls = {"n": 0}

    def failing(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            raise ExecutionError(code="test_kernel_failure", message="injected on chunk 2")
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunked_entry, "_mask_chunk_native", failing)
    oracle_calls: list[Any] = []
    monkeypatch.setattr(_chunked_oracle, "_oracle_masked", lambda *a, **k: oracle_calls.append(1))
    adapter_calls: list[Any] = []
    real_run = PandasExecutionAdapter.run
    monkeypatch.setattr(
        PandasExecutionAdapter,
        "run",
        lambda self, *a, **k: adapter_calls.append(1) or real_run(self, *a, **k),
    )
    writer = vault_writer_for_config(cfg)
    with pytest.raises(ExecutionError) as raised:
        support.run_default(cfg, src, vault_writer=writer)
    assert raised.value.code == "test_kernel_failure"
    assert calls["n"] == 3
    assert oracle_calls == [] and adapter_calls == []
    assert len(writer._entries) == 2 * support.CHUNK  # chunks 0 and 1 only


# ---------------------------------------------------------------------------
# Test 10: non-routed jobs never enter the new module.
# ---------------------------------------------------------------------------


def _two_table_job(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    src = pa.table({"val": pa.array([f"v{i}" for i in range(support.ROWS)])})
    cfg = support.make_cfg([support.hash_col("val", "a_ns")], table="a")
    cfg["tables"].append({"name": "b", "columns": [support.hash_col("val", "b_ns")]})
    cfg["sources"]["b"] = dict(cfg["sources"]["a"])
    cfg["targets"]["b"] = dict(cfg["targets"]["a"])
    for name in ("a", "b"):
        cfg["sources"][name]["path"] = support.write_source(src, tmp_path / f"{name}.parquet")
    return cfg, {"a": src, "b": src}


def _fk_job(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table({"id": pa.array([f"C{i}" for i in range(support.ROWS)])})
    child = pa.table({"customer_id": pa.array([f"C{i % 10}" for i in range(support.ROWS)])})
    cfg = support.make_cfg([support.hash_col("id", "id_ns")], table="customers")
    cfg["tables"].append({"name": "orders", "columns": [support.hash_col("customer_id", "id_ns")]})
    cfg["sources"]["orders"] = dict(cfg["sources"]["customers"])
    cfg["targets"]["orders"] = dict(cfg["targets"]["customers"])
    cfg["sources"]["customers"]["path"] = support.write_source(
        parent, tmp_path / "customers.parquet"
    )
    cfg["sources"]["orders"]["path"] = support.write_source(child, tmp_path / "orders.parquet")
    cfg["relationships"] = [
        {
            "parent": {"table": "customers", "columns": ["id"]},
            "children": [{"table": "orders", "columns": ["customer_id"]}],
            "orphan_policy": "preserve",
            "namespace": "id_ns",
        }
    ]
    return cfg, {"customers": parent, "orders": child}


@pytest.mark.parametrize("shape", ["below_threshold", "auto_chunk_off", "multi_table", "fk"])
def test_non_routed_jobs_never_enter_the_new_module(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    spies = support.spy_lanes(monkeypatch)
    if shape == "multi_table":
        cfg, sources = _two_table_job(tmp_path)
        kwargs = support.run_kwargs()
    elif shape == "fk":
        cfg, sources = _fk_job(tmp_path)
        kwargs = support.run_kwargs()
    else:
        cfg, src = _hash_redact_job(tmp_path)
        sources = {support.TABLE: src}
        kwargs = support.run_kwargs()
        if shape == "below_threshold":
            kwargs["auto_chunk_threshold_rows"] = 10**9
        else:
            kwargs["auto_chunk"] = False
    result = run_pipeline(cfg, sources=sources, **kwargs)
    assert result.quality_metrics.get("auto_chunk", {}).get("mode") != "chunked"
    assert "chunked_route" not in result.quality_metrics
    assert spies["auto_chunk.run_auto_chunk"] == []
    assert spies["entry.run_mask_chunked"] == []
    assert spies["route_exec.run_mask_chunked"] == []


# ---------------------------------------------------------------------------
# Test 11: A8 and lazy sources.
# ---------------------------------------------------------------------------


def test_a_large_lazy_source_is_not_routed_chunked_and_never_read_by_the_new_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.profile._readers import LazySource

    cfg, src = _hash_redact_job(tmp_path)
    path = Path(cfg["sources"][support.TABLE]["path"])
    spies = support.spy_lanes(monkeypatch)
    result = run_pipeline(
        cfg,
        sources={support.TABLE: LazySource(path)},
        **support.run_kwargs(explain_plan=True),
    )
    assert result.quality_metrics.get("auto_chunk", {}).get("mode") != "chunked"
    assert "lazy" in json.dumps(result.quality_metrics["execution_plan"]["rejections"]).lower()
    assert spies["auto_chunk.run_auto_chunk"] == []
    assert spies["entry.run_mask_chunked"] == []


def test_a_large_resident_transform_bearing_job_is_prepared_once_and_not_routed_chunked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _pipeline
    from tests.unit.execution._transform_testkit import base_table, single_table_config

    table = base_table(30)
    cfg = single_table_config(tmp_path, table, transforms=[{"op": "limit", "n": 25}])
    prepared: list[Any] = []
    real = _pipeline.prepare_transform_sources

    def spy(*args: Any, **kwargs: Any) -> Any:
        prepared.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(_pipeline, "prepare_transform_sources", spy)
    spies = support.spy_lanes(monkeypatch)
    result = run_pipeline(
        cfg,
        sources={"t": table},
        engine_version="b2-a8",
        auto_chunk_threshold_rows=1,
        chunk_size_rows=7,
        explain_plan=True,
    )
    assert len(prepared) == 1
    assert (
        "per_table_transforms_present"
        in result.quality_metrics["execution_plan"]["rejections"]["chunked"]
    )
    assert result.quality_metrics.get("auto_chunk", {}).get("mode") != "chunked"
    assert spies["auto_chunk.run_auto_chunk"] == []
    assert spies["entry.run_mask_chunked"] == []
