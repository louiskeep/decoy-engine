"""B1 rev9 acceptance tests 18 and 22: what the adapter sees with carried
columns, and custom and subclass adapters, which disable carrying (rule R1)."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
    truncate,
)
from tests.native._rev9_support import BY_NAME, companion_missing, run_entry, run_public

_INTS = pa.array([11, 22, 33], pa.int64())
_STAMPS = pa.array([1, None, 3], pa.timestamp("us"))


def _chunks(n: int = 3) -> list[pa.Table]:
    return [
        pa.table(
            {
                "s": pa.array([f"s{i}a", f"s{i}b", f"s{i}c"]),
                "x": pa.array([i, i + 1, None], pa.int64()),
                "ts": _STAMPS,
                "z": pa.array(["u", "v", "w"]),
            }
        )
        for i in range(n)
    ]


def _config(*, configured: bool, policy: str | None = None) -> dict[str, Any]:
    cols = [redact("s")]
    if configured:
        cols += [passthrough("x"), passthrough("ts"), passthrough("z")]
    gs = {"unconfigured_column_policy": policy} if policy else None
    return make_config(cols, global_settings=gs)


class _Recorder(PandasExecutionAdapter):
    """Stock behavior plus a record of the frame handed to `run`."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[pa.Table] = []

    def run(self, plan: Any, sources: Any, **kw: Any) -> Any:  # type: ignore[override]
        self.seen.append(sources[TABLE])
        return super().run(plan, sources, **kw)


# ---------------------------------------------------------------------------
# Test 18
# ---------------------------------------------------------------------------


def _hash_chunks(n: int = 3) -> list[pa.Table]:
    return [
        c.append_column("h", pa.array([f"h{i}a", f"h{i}b", f"h{i}c"]))
        for i, c in enumerate(_chunks(n))
    ]


def test_adapter_frame_has_the_source_column_names_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    real = PandasExecutionAdapter.run

    def run(self: Any, plan: Any, sources: Any, **kw: Any) -> Any:
        seen.append(list(sources[TABLE].column_names))
        return real(self, plan, sources, **kw)

    monkeypatch.setattr(PandasExecutionAdapter, "run", run)
    chunks = _hash_chunks()
    # A hash column with the companion missing forces the oracle route, so the adapter
    # still runs; the unconfigured columns x, ts and z reach it as source-named columns.
    with companion_missing(monkeypatch):
        _out, _sink, ev = run_entry(make_config([redact("s"), hash_col("h")]), chunks)
    assert ev[0].native_admitted is False
    assert ev[0].reroute_reason == "crypto_extension_unavailable"
    assert seen == [chunks[0].column_names] * 3


def test_undeclared_output_columns_error_is_raised_at_the_first_next() -> None:
    config = _config(configured=False, policy="error")
    with pytest.raises(ExecutionError) as oracle:
        run_public(config, _chunks())
    gen = run_mask_chunked(
        config,
        _chunks(),
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
    )
    with pytest.raises(ExecutionError) as entry:
        next(gen)
    assert entry.value.code == oracle.value.code == "undeclared_output_columns"
    assert str(entry.value) == str(oracle.value)


def test_warning_details_match_the_public_oracle_per_chunk() -> None:
    config = _config(configured=False, policy="warn")
    chunks = _chunks()
    oracle_sink: list[Any] = []
    run_public(config, chunks, chunk_result_sink=oracle_sink)
    _out, sink, _ev = run_entry(config, chunks)
    assert len(sink) == len(oracle_sink) == 3
    for got, want in zip(sink, oracle_sink, strict=True):
        assert [(w.code, w.detail) for w in got.warnings] == [
            (w.code, w.detail) for w in want.warnings
        ]
        assert got.warnings, "the unconfigured columns must still be reported"


@pytest.mark.parametrize("case", ["configured", "unconfigured", "forced_oracle"])
def test_timing_records_have_the_oracles_structure(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case == "forced_oracle":
        config = make_config(
            [redact("s"), hash_col("h")], global_settings={"unconfigured_column_policy": "warn"}
        )
        chunks = _hash_chunks()
    else:
        config = _config(configured=case == "configured", policy="warn")
        chunks = _chunks()
    oracle_sink: list[Any] = []
    run_public(config, chunks, chunk_result_sink=oracle_sink)
    if case == "forced_oracle":
        with companion_missing(monkeypatch):
            out, sink, ev = run_entry(config, chunks)
    else:
        out, sink, ev = run_entry(config, chunks)
    assert ev[0].native_admitted is (case != "forced_oracle")
    if case != "forced_oracle":
        # Native route: one record per configured column and none for an unconfigured one.
        expected = ["s", "ts", "x", "z"] if case == "configured" else ["s"]
        assert sorted(r.column for r in sink[0].timings) == expected
        return
    for got, want in zip(sink, oracle_sink, strict=True):
        assert [(r.column, r.strategy_type) for r in got.timings] == [
            (r.column, r.strategy_type) for r in want.timings
        ]
        assert got.boundary_conversion_ms >= 0.0
    assert len(out) == 3


# ---------------------------------------------------------------------------
# Test 22
# ---------------------------------------------------------------------------


class _ReadsX(_Recorder):
    """Overwrites masked `s` from passthrough `x` on the source table, then delegates."""

    def run(self, plan: Any, sources: Any, **kw: Any) -> Any:  # type: ignore[override]
        table = sources[TABLE]
        derived = pc.cast(pc.multiply(table.column("x"), 2), pa.string())
        table = table.set_column(table.schema.get_field_index("s"), "s", derived)
        return super().run(plan, {TABLE: table}, **kw)


def _subclass_config() -> dict[str, Any]:
    return make_config([truncate("s", 2), passthrough("x"), passthrough("ts")])


def _x_chunks() -> list[pa.Table]:
    return [
        pa.table(
            {
                "s": pa.array(["a", "b", "c"]),
                "x": pa.array([100 * (i + 1), 200 * (i + 1), None], pa.int64()),
                "ts": _STAMPS,
            }
        )
        for i in range(3)
    ]


def test_subclass_adapter_matches_the_public_oracle_and_sees_real_columns() -> None:
    chunks = _x_chunks()
    config = _subclass_config()
    oracle_adapter, entry_adapter = _ReadsX(), _ReadsX()
    oracle_sink: list[Any] = []
    expected = run_public(config, chunks, adapter=oracle_adapter, chunk_result_sink=oracle_sink)
    out, sink, ev = run_entry(config, chunks, adapter=entry_adapter)
    assert ev[0].native_admitted is False and ev[0].reroute_reason == "adapter_requested"
    assert [t.column("s").to_pylist() for t in out] == [t.column("s").to_pylist() for t in expected]
    assert expected[0].column("s").to_pylist()[0] == "20"
    for got, want in zip(sink, oracle_sink, strict=True):
        assert [(w.code, w.detail) for w in got.warnings] == [
            (w.code, w.detail) for w in want.warnings
        ]
        assert got.row_errors == want.row_errors
    for seen in entry_adapter.seen:
        assert not pa.types.is_null(seen.column("x").type)
        assert seen.column("x").null_count == 1
    assert [t.column("x").to_pylist() for t in entry_adapter.seen] == [
        t.column("x").to_pylist() for t in chunks
    ]
    listed = {tuple(r.quality_metrics["chunked_route"]["pandas_read_passthrough"]) for r in sink}
    assert listed == {("ts", "x")}
    assert aggregate_chunked_route_evidence(sink)["pandas_read_passthrough"] == ["ts", "x"]


@pytest.mark.parametrize("make", [_Recorder, _ReadsX], ids=["recorder", "reads_x"])
def test_subclass_adapter_passthrough_refusal_propagates_raw(make: Any) -> None:
    shape = BY_NAME["time64ns_unaligned"]
    chunks = [
        pa.table({"s": pa.array(["a", "b", "c"]), "x": _INTS, "t": shape.good}),
        pa.table({"s": pa.array(["a", "b", "c"]), "x": _INTS, "t": shape.bad}),
    ]
    config = make_config([truncate("s", 2), passthrough("x"), passthrough("t")])
    with pytest.raises(Exception) as oracle:
        run_public(config, chunks, adapter=make())
    gen = run_mask_chunked(
        config,
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        adapter=make(),
    )
    next(gen)
    with pytest.raises(Exception) as entry:
        next(gen)
    assert not isinstance(entry.value, ExecutionError)
    assert type(entry.value) is type(oracle.value) and str(entry.value) == str(oracle.value)


def test_row_errors_under_a_subclass_match_the_public_oracle() -> None:
    config = make_config(
        [
            {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
            passthrough("x"),
        ]
    )
    chunks = [pa.table({"age": pa.array(["23", "x1", "47"]), "x": _INTS})]
    with pytest.raises(RowErrorsFailedError) as oracle:
        run_public(config, chunks, adapter=_Recorder())
    with pytest.raises(RowErrorsFailedError) as entry:
        list(
            run_mask_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                adapter=_Recorder(),
            )
        )
    assert [r.trigger for r in entry.value.records] == [r.trigger for r in oracle.value.records]
