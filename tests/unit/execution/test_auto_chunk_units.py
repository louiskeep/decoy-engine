"""Unit tests for the free functions of `_pipeline_auto_chunk` and the delegate in
`_pipeline_route_exec`, written to kill mutants: lane selection, the knob checks,
the lane-stamp merge, the join rule, the evidence builders and the argument
forwarding. The acceptance tests (`test_auto_chunk_dispatcher`,
`test_auto_chunk_output_contract`) cover the same code through `run_pipeline`.
"""

from __future__ import annotations

import logging
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution import _pipeline_auto_chunk as ac
from decoy_engine.execution import _pipeline_route_exec as rx
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.providers_v2 import get_default_registry
from tests.unit.execution import _auto_chunk_support as support


def test_lane_names_are_the_recorded_strings() -> None:
    assert (ac.LANE_DISPATCHER, ac.LANE_LEGACY) == ("dispatcher", "legacy_oracle")
    assert ac.REASON_DISPATCHER_DISABLED == "dispatcher_disabled"


def test_select_lane() -> None:
    assert ac.select_lane(True) == ("dispatcher", None)
    assert ac.select_lane(False) == ("legacy_oracle", "dispatcher_disabled")


@pytest.mark.parametrize(("threads", "flag"), [(1, True), (4, False), (1024, True)])
def test_valid_lane_knobs_pass(threads: int, flag: bool) -> None:
    ac.require_lane_knobs(threads, flag)


@pytest.mark.parametrize("bad", [0, -1, 1025, 10**6, True, False, "2", 2.5, None])
def test_invalid_native_threads_raise_the_knob_error(bad: Any) -> None:
    with pytest.raises(ExecutionError) as raised:
        ac.require_lane_knobs(bad, True)
    assert raised.value.code == "invalid_execution_knob"
    assert "native_threads must be" in str(raised.value)


def test_the_upper_bound_message_names_the_limit_and_the_value() -> None:
    with pytest.raises(ExecutionError) as raised:
        ac.require_lane_knobs(1025, True)
    assert "1024" in str(raised.value) and "1025" in str(raised.value)


@pytest.mark.parametrize("bad", ["false", 1, 0, None])
def test_invalid_dispatcher_flag_raises_the_knob_error(bad: Any) -> None:
    with pytest.raises(ExecutionError) as raised:
        ac.require_lane_knobs(1, bad)
    assert raised.value.code == "invalid_execution_knob"
    assert "chunked_dispatcher_enabled must be" in str(raised.value)


def test_native_threads_is_checked_before_the_dispatcher_flag() -> None:
    with pytest.raises(ExecutionError) as raised:
        ac.require_lane_knobs(0, "nope")
    assert "native_threads must be" in str(raised.value)


def test_merge_lane_stamp_keeps_the_six_keys_and_adds_the_lane_keys() -> None:
    stamp = {"mode": "chunked", "chunk_count": 4}
    routed = {"auto_chunk": {"lane": "dispatcher", "lane_reason": None, "native_threads": 3}}
    merged = ac.merge_lane_stamp(stamp, routed)
    assert merged == {
        "mode": "chunked",
        "chunk_count": 4,
        "lane": "dispatcher",
        "lane_reason": None,
        "native_threads": 3,
    }
    assert stamp == {"mode": "chunked", "chunk_count": 4}, "the input stamp is not mutated"


def test_merge_lane_stamp_lane_keys_win_a_tie_and_an_absent_block_adds_nothing() -> None:
    assert ac.merge_lane_stamp({"mode": "a"}, {"auto_chunk": {"mode": "b"}}) == {"mode": "b"}
    base = {"mode": "full_frame"}
    merged = ac.merge_lane_stamp(base, {"other": 1})
    assert merged == base and merged is not base


def test_without_elapsed_drops_only_elapsed_ms_and_leaves_the_input_alone() -> None:
    evidence = {
        "table": "t",
        "native_admitted": True,
        "columns": [
            {"column": "a", "calls": 2, "elapsed_ms": 1.5},
            {"column": "b", "elapsed_ms": 0},
        ],
    }
    out = ac._without_elapsed(evidence)
    assert out == {
        "table": "t",
        "native_admitted": True,
        "columns": [{"column": "a", "calls": 2}, {"column": "b"}],
    }
    assert evidence["columns"][0]["elapsed_ms"] == 1.5


def test_slices_are_zero_copy_and_cover_every_row_once() -> None:
    table = pa.table({"a": list(range(10))})
    parts = list(ac._slices(table, 4))
    assert [p.num_rows for p in parts] == [4, 4, 2]
    assert pa.concat_tables(parts).equals(table)
    assert parts[1].column("a").chunk(0).buffers()[1].address == (
        table.column("a").chunk(0).buffers()[1].address
    )


# ---------------------------------------------------------------------------
# join_dispatcher_chunks: cases beyond the acceptance test's.
# ---------------------------------------------------------------------------


def test_join_of_a_single_chunk_is_that_chunk_without_schema_metadata() -> None:
    chunk = pa.table({"a": ["x", "y"]}).replace_schema_metadata({b"k": b"v"})
    joined = ac.join_dispatcher_chunks([chunk], table="t")
    assert joined.column("a").to_pylist() == ["x", "y"]
    assert joined.schema.metadata is None


def test_join_keeps_column_order_and_rejects_a_reordered_chunk() -> None:
    first = pa.table({"a": ["x"], "b": ["y"]})
    joined = ac.join_dispatcher_chunks([first, first], table="t")
    assert joined.column_names == ["a", "b"] and joined.num_rows == 2
    with pytest.raises(ExecutionError) as raised:
        ac.join_dispatcher_chunks([first, pa.table({"b": ["y"], "a": ["x"]})], table="t")
    assert raised.value.code == "chunked_schema_mismatch"


def test_join_error_messages_name_the_table_the_column_and_the_disagreement() -> None:
    with pytest.raises(ExecutionError) as raised:
        ac.join_dispatcher_chunks([pa.table({"a": ["x"]}), pa.table({"a": [1]})], table="people")
    message = str(raised.value)
    assert "people" in message and "'a'" in message
    assert "disagreeing types" in message and "string" in message and "int64" in message
    with pytest.raises(ExecutionError) as raised:
        ac.join_dispatcher_chunks([pa.table({"a": ["x"]}), pa.table({"b": ["x"]})], table="people")
    message = str(raised.value)
    assert "people" in message and "column names differ across chunks" in message
    assert "['a']" in message and "['b']" in message
    with pytest.raises(ExecutionError) as raised:
        ac.join_dispatcher_chunks([], table="people")
    assert str(raised.value).endswith("table 'people': no chunks to join.")


def test_join_takes_the_non_null_type_from_any_position_and_casts_the_rest() -> None:
    nulls = pa.table({"a": pa.nulls(1)})
    typed = pa.table({"a": pa.array(["x"], pa.large_string())})
    joined = ac.join_dispatcher_chunks([nulls, typed, nulls], table="t")
    assert joined.schema.field("a").type == pa.large_string()
    assert joined.column("a").to_pylist() == [None, "x", None]


def test_join_with_fields_that_differ_only_in_metadata_keeps_the_first_chunks_field() -> None:
    plain = pa.field("a", pa.string(), metadata={b"owner": b"one"})
    other = pa.field("a", pa.string(), metadata={b"owner": b"two"})
    chunks = [
        pa.Table.from_arrays([pa.array(["x"])], schema=pa.schema([plain])),
        pa.Table.from_arrays([pa.array(["y"])], schema=pa.schema([other])),
    ]
    joined = ac.join_dispatcher_chunks(chunks, table="t")
    assert joined.schema.field("a").equals(plain, check_metadata=True)
    assert joined.column("a").to_pylist() == ["x", "y"]


def test_join_of_null_typed_fields_that_differ_only_in_metadata_keeps_the_first_field() -> None:
    first = pa.field("a", pa.null(), metadata={b"owner": b"one"})
    second = pa.field("a", pa.null(), metadata={b"owner": b"two"})
    chunks = [
        pa.Table.from_arrays([pa.nulls(1)], schema=pa.schema([first])),
        pa.Table.from_arrays([pa.nulls(2)], schema=pa.schema([second])),
    ]
    joined = ac.join_dispatcher_chunks(chunks, table="t")
    assert joined.schema.field("a").equals(first, check_metadata=True)
    assert joined.num_rows == 3


def test_join_returns_one_contiguous_chunk_per_column() -> None:
    chunks = [pa.table({"a": ["x"], "b": [1]}) for _ in range(3)]
    joined = ac.join_dispatcher_chunks(chunks, table="t")
    assert all(joined.column(n).num_chunks == 1 for n in joined.column_names)


# ---------------------------------------------------------------------------
# run_auto_chunk and the delegate.
# ---------------------------------------------------------------------------


def _native_table() -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(
        {
            "r": pa.array([f"s{i}" for i in range(support.ROWS)]),
            "p": pa.array([f"keep-{i}" for i in range(support.ROWS)]),
        }
    )
    cfg = support.make_cfg([support.redact_col("r"), support.pass_col("p")])
    return cfg, src


def _call(cfg: dict[str, Any], src: pa.Table, **kw: Any) -> Any:
    from decoy_engine.providers_v2 import get_default_registry

    params: dict[str, Any] = {
        "table": support.TABLE,
        "engine_version": support.ENGINE_VERSION,
        "registry": get_default_registry(),
        "adapter": PandasExecutionAdapter(),
        "vault_writer": None,
        "chunk_size_rows": support.CHUNK,
        "key_provider": None,
        "native_threads": 2,
        "dispatcher_enabled": True,
    }
    params.update(kw)
    return ac.run_auto_chunk(cfg, src, **params)


def test_run_auto_chunk_dispatcher_lane_returns_the_five_part_shape() -> None:
    cfg, src = _native_table()
    outputs, timings, conversion_ms, warnings, metrics = _call(cfg, src)
    assert list(outputs) == ["t"] and outputs["t"].num_rows == support.ROWS
    assert isinstance(timings, tuple) and {t.column for t in timings} == {"r", "p"}
    assert conversion_ms == 0.0
    assert warnings == ()
    assert metrics["auto_chunk"] == {"lane": "dispatcher", "lane_reason": None, "native_threads": 2}
    route = metrics["chunked_route"]
    assert route["table"] == "t"
    assert route["native_admitted"] is True and route["reroute_reason"] is None
    assert route["pandas_read_passthrough"] == []
    assert [c["column"] for c in route["columns"]] == ["r", "p"]
    assert all(c["calls"] == 3 and "elapsed_ms" not in c for c in route["columns"])
    assert "code_set_corpora" not in metrics


def test_run_auto_chunk_legacy_lane_reports_the_same_shape_on_pandas() -> None:
    cfg, src = _native_table()
    outputs, timings, conversion_ms, _warnings, metrics = _call(
        cfg, src, dispatcher_enabled=False, native_threads=7
    )
    assert outputs["t"].num_rows == support.ROWS and conversion_ms > 0.0
    assert {t.column for t in timings} == {"r", "p"}
    assert metrics["auto_chunk"] == {
        "lane": "legacy_oracle",
        "lane_reason": "dispatcher_disabled",
        "native_threads": 7,
    }
    route = metrics["chunked_route"]
    assert route["table"] == "t"
    assert route["native_admitted"] is False
    assert route["reroute_reason"] == "dispatcher_disabled"
    assert route["pandas_read_passthrough"] == ["p"]
    assert {
        c["column"]: (c["planned_backend"], c["executed_backend"], c["calls"])
        for c in route["columns"]
    } == {
        "r": ("arrow_python", "pandas_oracle", 3),
        "p": ("arrow_python", "pandas_oracle", 3),
    }


def test_run_auto_chunk_logs_the_lane_without_values(caplog: pytest.LogCaptureFixture) -> None:
    cfg, src = _native_table()
    with caplog.at_level(logging.INFO, logger=ac.__name__):
        _call(cfg, src)
        _call(cfg, src, dispatcher_enabled=False)
    messages = [r.getMessage() for r in caplog.records if r.name == ac.__name__]
    assert messages == [
        "auto-chunk table=t lane=dispatcher native_admitted=True reroute_reason=None",
        "auto-chunk table=t lane=legacy_oracle native_admitted=False "
        "reroute_reason=dispatcher_disabled",
    ]


def test_the_dispatcher_lane_forwards_the_run_context_to_b1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.providers_v2 import get_default_registry

    cfg, src = _native_table()
    spies = support.spy_lanes(monkeypatch)
    registry, adapter = get_default_registry(), PandasExecutionAdapter()
    _call(cfg, src, registry=registry, adapter=adapter, native_threads=3)
    ((args, kwargs),) = spies["entry.run_mask_chunked"]
    assert args[0] is cfg
    assert kwargs["table"] == "t" and kwargs["engine_version"] == support.ENGINE_VERSION
    assert kwargs["registry"] is registry and kwargs["adapter"] is adapter
    assert kwargs["vault_writer"] is None and kwargs["key_provider"] is None
    assert kwargs["base_row_offset"] == 0 and kwargs["native_threads"] == 3
    assert isinstance(kwargs["chunk_result_sink"], list)
    assert spies["oracle.run_mask_pipeline_chunked"] == []


def test_the_legacy_lane_forwards_the_run_context_to_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.keyprovider import SecretKeyProvider
    from decoy_engine.providers_v2 import get_default_registry

    cfg, src = _native_table()
    spies = support.spy_lanes(monkeypatch)
    registry, adapter = get_default_registry(), PandasExecutionAdapter()
    provider = SecretKeyProvider(secret=bytes(range(32)), key_version="v1")
    _call(
        cfg,
        src,
        dispatcher_enabled=False,
        registry=registry,
        adapter=adapter,
        key_provider=provider,
    )
    ((args, kwargs),) = spies["oracle.run_mask_pipeline_chunked"]
    assert args[0] is cfg
    assert kwargs["table"] == "t" and kwargs["engine_version"] == support.ENGINE_VERSION
    assert kwargs["registry"] is registry and kwargs["adapter"] is adapter
    assert kwargs["key_provider"] is provider and kwargs["vault_writer"] is None
    assert isinstance(kwargs["chunk_result_sink"], list)
    assert spies["entry.run_mask_chunked"] == []


def test_the_legacy_evidence_is_planned_from_this_tables_config_and_engine_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution import _chunked_profile
    from decoy_engine.execution.native import _chunked_evidence

    seen: dict[str, Any] = {}
    real_profile = _chunked_profile.first_chunk_profile
    real_plan = _chunked_evidence.plan_column_backends

    def spy_profile(config: Any, **kw: Any) -> Any:
        seen["profile"] = kw
        return real_profile(config, **kw)

    def spy_plan(config: Any, profile: Any, **kw: Any) -> Any:
        seen["plan"] = kw
        return real_plan(config, profile, **kw)

    monkeypatch.setattr(_chunked_profile, "first_chunk_profile", spy_profile)
    monkeypatch.setattr(_chunked_evidence, "plan_column_backends", spy_plan)
    cfg, src = _native_table()
    ac._legacy_route_evidence(
        cfg,
        src,
        table="t",
        engine_version="ev-1",
        chunk_size_rows=16,
        chunk_count=3,
        lane_reason="dispatcher_disabled",
        registry=get_default_registry(),
    )
    assert seen["profile"] == {"table": "t", "engine_version": "ev-1"}
    assert seen["plan"] == {"table": "t", "engine_version": "ev-1"}


def test_the_legacy_lane_plans_its_evidence_with_the_run_engine_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution.native import _chunked_evidence

    seen: list[dict[str, Any]] = []
    real = _chunked_evidence.plan_column_backends

    def spy(config: Any, profile: Any, **kw: Any) -> Any:
        seen.append(kw)
        return real(config, profile, **kw)

    monkeypatch.setattr(_chunked_evidence, "plan_column_backends", spy)
    cfg, src = _native_table()
    _call(cfg, src, dispatcher_enabled=False, engine_version="ev-2")
    assert seen == [{"table": "t", "engine_version": "ev-2"}]


def _schema_disagreement(monkeypatch: pytest.MonkeyPatch, target: Any, name: str) -> None:
    chunks = [pa.table({"a": ["x"]}), pa.table({"a": [1]})]
    monkeypatch.setattr(target, name, lambda *a, **k: iter(chunks))


def test_a_join_failure_on_the_dispatcher_lane_names_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution.native import _chunked_entry

    _schema_disagreement(monkeypatch, _chunked_entry, "run_mask_chunked")
    cfg, src = _native_table()
    with pytest.raises(ExecutionError) as raised:
        _call(cfg, src, table="people")
    assert raised.value.code == "chunked_schema_mismatch" and "people" in str(raised.value)


def test_a_join_failure_on_the_legacy_lane_names_the_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution import _chunked

    _schema_disagreement(monkeypatch, _chunked, "run_mask_pipeline_chunked")
    cfg, src = _native_table()
    with pytest.raises(ExecutionError) as raised:
        _call(cfg, src, table="people", dispatcher_enabled=False)
    assert raised.value.code == "chunked_schema_mismatch" and "people" in str(raised.value)


def test_the_delegate_forwards_every_argument_and_returns_the_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    seen: dict[str, Any] = {}

    def fake(config: Any, source: Any, **kwargs: Any) -> Any:
        seen.update({"config": config, "source": source, **kwargs})
        return sentinel

    monkeypatch.setattr(ac, "run_auto_chunk", fake)
    markers = {
        name: object() for name in ("config", "source", "registry", "adapter", "writer", "keys")
    }
    result = rx.run_mask_chunked(
        markers["config"],  # type: ignore[arg-type]
        markers["source"],  # type: ignore[arg-type]
        table="t",
        engine_version="v",
        registry=markers["registry"],  # type: ignore[arg-type]
        adapter=markers["adapter"],
        vault_writer=markers["writer"],
        chunk_size_rows=17,
        key_provider=markers["keys"],  # type: ignore[arg-type]
        native_threads=5,
        dispatcher_enabled=False,
    )
    assert result is sentinel
    assert seen["config"] is markers["config"] and seen["source"] is markers["source"]
    assert seen["registry"] is markers["registry"] and seen["adapter"] is markers["adapter"]
    assert seen["vault_writer"] is markers["writer"] and seen["key_provider"] is markers["keys"]
    assert (seen["table"], seen["engine_version"], seen["chunk_size_rows"]) == ("t", "v", 17)
    assert seen["native_threads"] == 5 and seen["dispatcher_enabled"] is False
    assert set(seen) == {
        "config", "source", "table", "engine_version", "registry", "adapter",
        "vault_writer", "chunk_size_rows", "key_provider", "native_threads", "dispatcher_enabled",
    }  # fmt: skip


def test_the_delegate_defaults_are_one_thread_and_the_dispatcher_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(ac, "run_auto_chunk", lambda *a, **k: seen.update(k))
    rx.run_mask_chunked(
        {}, pa.table({"a": [1]}), table="t", engine_version="v", registry=None,  # type: ignore[arg-type]
        adapter=None, vault_writer=None, chunk_size_rows=1,
    )  # fmt: skip
    assert seen["native_threads"] == 1 and seen["dispatcher_enabled"] is True
    assert seen["key_provider"] is None
