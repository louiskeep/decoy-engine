"""B1 rev9 acceptance tests 15, 16 and 19: carried passthrough columns.

`run_mask_chunked` never converts a carried passthrough column to pandas, so a
value the pandas round trip refuses or alters comes back exactly as the source
held it, on either route and in any chunk. The public oracle keeps its
behavior, pinned per shape in `_rev9_support`.
"""

from __future__ import annotations

from typing import Any

import pandas
import pyarrow as pa
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _pandas_adapter
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.profile import _walk
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    make_config,
    passthrough,
    redact,
)
from tests.native._rev9_support import (
    BY_NAME,
    I64MIN,
    SHAPE_IDS,
    SHAPES,
    Shape,
    companion_missing,
    config_for,
    ipc_bytes,
    run_entry,
    run_public,
    same_column,
    stream,
)

PINNED_UNDER = f"pandas {pandas.__version__}, pyarrow {pa.__version__}"
_ROUTES = ["native", "oracle"]


def _expect_route(route: str, configured: bool) -> bool:
    # Unconfigured columns under the pre-GA `warn` default no longer veto the native
    # route; only a forced route is the oracle.
    return route == "native"


def _entry(
    shape: Shape, pos: int, configured: bool, route: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[pa.Table], list[Any], list[Any], list[pa.Table]]:
    forced = route == "oracle"
    config = config_for(configured=configured, with_hash=forced)
    chunks = stream(shape, pos, with_hash=forced)
    if forced:
        with companion_missing(monkeypatch):
            out, sink, ev = run_entry(config, chunks)
    else:
        out, sink, ev = run_entry(config, chunks)
    return out, sink, ev, chunks


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize("configured", [True, False], ids=["configured", "unconfigured"])
@pytest.mark.parametrize("pos", [0, 2])
@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
def test_carried_passthrough_is_returned_exactly_on_both_routes(
    shape: Shape, pos: int, configured: bool, route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    out, sink, ev, chunks = _entry(shape, pos, configured, route, monkeypatch)
    assert ev[0].native_admitted is _expect_route(route, configured), (route, ev[0])
    assert len(out) == 3
    for got, src in zip(out, chunks, strict=True):
        assert same_column(got.column("x"), src.column("x")), (shape.name, PINNED_UNDER)
        assert got.column("s").to_pylist() == ["REDACTED"] * 3
    assert len(sink) == 3
    for result, src in zip(sink, chunks, strict=True):
        assert same_column(result.outputs[TABLE].column("x"), src.column("x"))


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "unconfigured"])
@pytest.mark.parametrize("pos", [0, 2])
@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
def test_both_routes_yield_equal_tables(
    shape: Shape, pos: int, configured: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, *_ = _entry(shape, pos, configured, "native", monkeypatch)
    oracle, *_ = _entry(shape, pos, configured, "oracle", monkeypatch)
    for a, b in zip(native, oracle, strict=True):
        left, right = a.select(["s", "x"]), b.select(["s", "x"])
        assert left.schema == right.schema
        assert left.column("s").to_pylist() == right.column("s").to_pylist()
        assert same_column(left.column("x"), right.column("x"))


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "unconfigured"])
@pytest.mark.parametrize("pos", [0, 2])
@pytest.mark.parametrize("shape", SHAPES, ids=SHAPE_IDS)
def test_public_oracle_behavior_is_unchanged(shape: Shape, pos: int, configured: bool) -> None:
    """The documented exception: what the public oracle still refuses or alters."""
    config = config_for(configured=configured)
    chunks = stream(shape, pos)
    if shape.public not in ("exact", "altered"):
        with pytest.raises(Exception) as info:
            run_public(config, chunks)
        assert type(info.value).__name__ == shape.public, PINNED_UNDER
        return
    out = run_public(config, chunks)
    for i, (got, src) in enumerate(zip(out, chunks, strict=True)):
        if shape.public == "exact" or i != pos:
            assert same_column(got.column("x"), src.column("x")), PINNED_UNDER
        else:
            # `-2^63` is NaT to pandas: row 0 comes back null, nothing else changes.
            assert got.column("x").is_null().to_pylist() == [True, True, False], PINNED_UNDER
            assert got.column("x").type == src.column("x").type
            assert got.column("x")[2].as_py() == src.column("x")[2].as_py()


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
@pytest.mark.parametrize("kind", ["timestamp", "duration"])
def test_min_int64_is_yielded_itself_and_the_oracle_nulls_it(kind: str, unit: str) -> None:
    shape = BY_NAME[f"{kind}_{unit}_min"]
    chunks = stream(shape, 1)
    for configured in (True, False):
        config = config_for(configured=configured)
        out, *_ = run_entry(config, chunks)
        stored = out[1].column("x").cast(pa.int64())
        assert stored[0].as_py() == I64MIN
        assert run_public(config, chunks)[1].column("x").is_null().to_pylist() == [
            True,
            True,
            False,
        ]


# ---------------------------------------------------------------------------
# Test 16: no pandas conversion of a carried column.
# ---------------------------------------------------------------------------


def _all_shapes_stream(with_hash: bool) -> tuple[list[pa.Table], list[str]]:
    names = [f"x{i}" for i in range(len(SHAPES))]
    chunks = []
    for c in range(3):
        cols: dict[str, Any] = {"s": pa.array([f"s{c}a", f"s{c}b", f"s{c}c"])}
        for name, shape in zip(names, SHAPES, strict=True):
            cols[name] = shape.bad
        if with_hash:
            cols["h"] = pa.array([f"h{c}a", f"h{c}b", f"h{c}c"])
        chunks.append(pa.table(cols))
    return chunks, names


class _Spy:
    def __init__(self) -> None:
        self.adapter_types: list[dict[str, pa.DataType]] = []
        self.fk_safe_types: list[dict[str, pa.DataType]] = []
        self.walk_values: list[dict[str, list[Any]]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = self
        real_run = PandasExecutionAdapter.run
        real_conv = _pandas_adapter.to_pandas_fk_safe
        real_walk = _walk.walk_dataframe

        def run(self: Any, plan: Any, sources: Any, **kw: Any) -> Any:
            spy.adapter_types.append({f.name: f.type for f in sources[TABLE].schema})
            return real_run(self, plan, sources, **kw)

        def conv(table: pa.Table, fk: Any) -> Any:
            spy.fk_safe_types.append({f.name: f.type for f in table.schema})
            return real_conv(table, fk)

        def walk(df: Any, *a: Any, **kw: Any) -> Any:
            spy.walk_values.append({c: list(df[c]) for c in df.columns})
            return real_walk(df, *a, **kw)

        monkeypatch.setattr(PandasExecutionAdapter, "run", run)
        monkeypatch.setattr(_pandas_adapter, "to_pandas_fk_safe", conv)
        monkeypatch.setattr(_walk, "walk_dataframe", walk)


def _is_none(v: Any) -> bool:
    return v is None or v is pandas.NA or (isinstance(v, float) and v != v)


@pytest.mark.parametrize("adapter_kind", ["none", "explicit_pandas_adapter"])
@pytest.mark.parametrize("route", _ROUTES)
def test_no_carried_column_reaches_pandas(
    route: str, adapter_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    forced = route == "oracle"
    chunks, names = _all_shapes_stream(with_hash=forced)
    cols = [redact("s")] + [passthrough(n) for n in names]
    if forced:
        from tests.native._chunked_entry_support import hash_col

        cols.append(hash_col("h"))
    config = make_config(cols)
    spy = _Spy()
    spy.install(monkeypatch)
    kwargs: dict[str, Any] = {}
    if adapter_kind == "explicit_pandas_adapter":
        kwargs["adapter"] = PandasExecutionAdapter()
    if forced:
        with companion_missing(monkeypatch):
            out, _sink, ev = run_entry(config, chunks, **kwargs)
    else:
        out, _sink, ev = run_entry(config, chunks, **kwargs)
    assert ev[0].native_admitted is (not forced)
    assert len(out) == 3
    # The profile walk saw every carried column as an all-null column.
    assert spy.walk_values, "the first-chunk profile must still run"
    for frame in spy.walk_values:
        for name in names:
            assert all(_is_none(v) for v in frame[name]), name
    if forced:
        assert len(spy.adapter_types) == 3
        for types in spy.adapter_types + spy.fk_safe_types:
            for name in names:
                assert pa.types.is_null(types[name]), (name, types[name])
    else:
        assert spy.adapter_types == [] and spy.fk_safe_types == []
    for got, src in zip(out, chunks, strict=True):
        for name in names:
            assert same_column(got.column(name), src.column(name)), name


def test_public_oracle_under_the_same_spy_receives_real_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = pa.table({"s": pa.array(["a", "b", "c"]), "x": pa.array([1, 2, 3], pa.int64())})
    spy = _Spy()
    spy.install(monkeypatch)
    run_public(make_config([redact("s"), passthrough("x")]), [table])
    assert spy.adapter_types == [{"s": pa.string(), "x": pa.int64()}]
    assert spy.fk_safe_types and spy.fk_safe_types[0]["x"] == pa.int64()
    assert [v for v in spy.walk_values[0]["x"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Test 19: a row-error chunk keeps the real carried column in the sink.
# ---------------------------------------------------------------------------

_ROW_ERROR_SHAPES = ["time64ns_unaligned", "dict_uint64_indices", "list_int", "date32_far"]


def _row_error_config(strategy: str, configured: bool) -> dict[str, Any]:
    if strategy == "bucketize":
        col: dict[str, Any] = {
            "name": "age",
            "strategy": "bucketize",
            "provider_config": {"width": 10},
        }
    else:
        col = {
            "name": "age",
            "strategy": "date_shift",
            "namespace": "age_ns",
            "provider_config": {"min_days": 30, "max_days": 60},
        }
    return make_config([col] + ([passthrough("x")] if configured else []))


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "unconfigured"])
@pytest.mark.parametrize("strategy", ["bucketize", "date_shift"])
@pytest.mark.parametrize("shape_name", _ROW_ERROR_SHAPES)
def test_row_error_chunk_sink_result_carries_the_source_column(
    shape_name: str, strategy: str, configured: bool
) -> None:
    shape = BY_NAME[shape_name]
    good, bad = ("23", "not-a-number") if strategy == "bucketize" else ("2020-01-01", "not-a-date")
    chunks = [
        pa.table({"age": pa.array([good, good, good]), "x": shape.good}),
        pa.table({"age": pa.array([good, bad, good]), "x": shape.bad}),
    ]
    sink: list[Any] = []
    from decoy_engine import run_mask_chunked
    from tests.native._chunked_entry_support import key_provider

    gen = run_mask_chunked(
        _row_error_config(strategy, configured),
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        chunk_result_sink=sink,
    )
    with pytest.raises(RowErrorsFailedError):
        list(gen)
    assert len(sink) == 2
    failing = sink[1]
    assert failing.row_errors
    column = failing.outputs[TABLE].column("x")
    assert not pa.types.is_null(column.type)
    assert same_column(column, chunks[1].column("x"))
    assert ipc_bytes(column) == ipc_bytes(chunks[1].column("x"))
