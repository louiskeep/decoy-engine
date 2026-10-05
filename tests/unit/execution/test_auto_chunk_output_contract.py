"""Acceptance tests 3, 3a, 3b of plan 2026-10-01-dispatcher-auto-chunk (rev 5):
the dispatcher lane's output contract (guarantee 3), the pandas-origin sources
that revision 3.1 sent to the legacy lane, `time64[ns]`, and the join rule.

Each case runs the same job on the dispatcher lane (default knobs), on today's
lane (`chunked_dispatcher_enabled=False`) and on the forced full frame, and
checks the contract through `check_contract`. A case also asserts the lane and
the B1 route it expects, so it cannot pass by quietly moving lanes. Do not
delete a case, skip one outside `NEEDS_COMPANION`, or weaken a comparison
without a new plan gate: a failing case is a defect in the code or a finding
for the plan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError, _chunked
from tests.unit.execution import _auto_chunk_matrix as matrix
from tests.unit.execution import _auto_chunk_strategies as strategies
from tests.unit.execution import _auto_chunk_support as support

COMPANION_PARAMS = [
    pytest.param("present", marks=support.NEEDS_COMPANION),
    "absent",
]


@pytest.fixture(autouse=True)
def _companion_state(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if "companion" in request.fixturenames and request.getfixturevalue("companion") == "absent":
        support.remove_companion(monkeypatch)


def _trio(
    cfg: dict[str, Any], src: pa.Table, *, threads: int = 1, **extra: Any
) -> tuple[Any, Any, Any]:
    dispatcher = support.run_default(cfg, src, native_threads=threads, **extra)
    legacy = support.run_legacy(cfg, src, **extra)
    full = support.run_full_frame(cfg, src, **extra)
    return dispatcher, legacy, full


def _assert_lane(result: Any, *, native: bool, reason_prefix: str | None = None) -> dict[str, Any]:
    block = result.quality_metrics["auto_chunk"]
    assert block["mode"] == "chunked"
    assert block["lane"] == "dispatcher"
    assert block["lane_reason"] is None
    evidence = result.quality_metrics["chunked_route"]
    assert evidence["native_admitted"] is native
    if native:
        assert evidence["reroute_reason"] is None
    else:
        assert evidence["reroute_reason"] is not None
        if reason_prefix is not None:
            assert evidence["reroute_reason"].startswith(reason_prefix)
    return evidence


# ---------------------------------------------------------------------------
# The strategy fixtures track the live admission set.
# ---------------------------------------------------------------------------


def test_strategy_fixture_keys_equal_the_live_chunk_admission_set() -> None:
    live = _chunked._CHUNK_ADMITTED_STRATEGIES | _chunked.CHUNK_CONDITIONAL_STRATEGIES
    assert {key.split(":")[0] for key in strategies.STRATEGY_FIXTURES} == set(live), (
        "a strategy was admitted or removed: add or drop its fixture in _auto_chunk_strategies"
    )
    # Conditional and gated strategies carry an explicit variant in the key.
    for strategy in ("faker", "categorical", "code_set", "bucket_perturb", "date_shift"):
        assert any(k.startswith(f"{strategy}:") for k in strategies.STRATEGY_FIXTURES), strategy


# ---------------------------------------------------------------------------
# Test 3: every admitted strategy x companion x native_threads.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize("key", sorted(strategies.STRATEGY_FIXTURES))
def test_output_contract_per_strategy(
    key: str, companion: str, threads: int, tmp_path: Path
) -> None:
    columns, data = strategies.STRATEGY_FIXTURES[key]
    src = pa.table(data)
    cfg = support.make_cfg(columns, path=support.write_source(src, tmp_path / "s.parquet"))
    dispatcher, legacy, full = _trio(cfg, src, threads=threads)

    present = companion == "present"
    native = key in strategies.NATIVE_KEYS and (
        present or key not in strategies.NEEDS_COMPANION_KEYS
    )
    if native:
        evidence = _assert_lane(dispatcher, native=True)
    elif key in strategies.NATIVE_KEYS:
        reason = {
            "hash": "crypto_extension_unavailable",
            "group_key": "raw_hex_extension_unavailable",
        }.get(key, "index_extension_unavailable")
        evidence = _assert_lane(dispatcher, native=False, reason_prefix=reason)
    else:
        evidence = _assert_lane(dispatcher, native=False, reason_prefix=strategies.REFUSAL[key])

    by_column = {c["column"]: c for c in evidence["columns"]}
    chunk_count = dispatcher.quality_metrics["auto_chunk"]["chunk_count"]
    assert by_column["val"]["calls"] == chunk_count
    planned = strategies.PLANNED_BACKEND.get(key, "pandas_oracle")
    assert by_column["val"]["planned_backend"] == planned
    assert by_column["val"]["executed_backend"] == (planned if native else "pandas_oracle")

    masked_names = {"val"} if key != "passthrough" else set()
    support.check_contract(
        dispatcher.outputs[support.TABLE],
        legacy.outputs[support.TABLE],
        full.outputs[support.TABLE],
        src,
        string_output=masked_names if key in strategies.STRING_OUTPUT_KEYS else set(),
        masked=masked_names if key not in strategies.STRING_OUTPUT_KEYS else set(),
    )


# ---------------------------------------------------------------------------
# Test 3: shapes that stress the join and the schema rule.
# ---------------------------------------------------------------------------


def _masked_trio_columns() -> list[dict[str, Any]]:
    return [
        support.hash_col("h"),
        support.redact_col("r"),
        support.truncate_col("z"),
        support.pass_col("p"),
    ]


def _base(n: int = support.ROWS) -> dict[str, pa.Array]:
    return {
        "h": pa.array([f"u{i}@x.example" for i in range(n)]),
        "r": pa.array([f"s{i}" for i in range(n)]),
        "z": pa.array([f"{i:05d}" for i in range(n)]),
        "p": pa.array([f"keep-{i}" for i in range(n)]),
    }


def _nulled(arr: pa.Array, rows: list[int]) -> pa.Array:
    mask = np.zeros(len(arr), dtype=bool)
    mask[rows] = True
    return pc.if_else(pa.array(mask), pa.scalar(None, arr.type), arr)


SHAPES: dict[str, dict[str, Any]] = {
    "uneven_last_chunk": {},
    "chunk0_all_null_then_values": {
        "h": lambda a: _nulled(a, list(range(support.CHUNK))),
        "z": lambda a: _nulled(a, list(range(support.CHUNK))),
    },
    "column_all_null_in_every_chunk": {
        "h": lambda a: _nulled(a, list(range(support.ROWS))),
        "r": lambda a: _nulled(a, list(range(support.ROWS))),
        "z": lambda a: _nulled(a, list(range(support.ROWS))),
    },
    "nulls_throughout": {
        "h": lambda a: _nulled(a, [0, 5, 17, 18, 39]),
        "r": lambda a: _nulled(a, [1, 16, 32]),
        "p": lambda a: _nulled(a, [2, 20, 38]),
    },
}


@pytest.mark.parametrize("threads", [1, 4])
@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_output_contract_shapes(shape: str, companion: str, threads: int, tmp_path: Path) -> None:
    cols = _base()
    for name, edit in SHAPES[shape].items():
        cols[name] = edit(cols[name])
    src = support.table_of(cols)
    cfg = support.make_cfg(
        _masked_trio_columns(), path=support.write_source(src, tmp_path / "s.parquet")
    )
    dispatcher, legacy, full = _trio(cfg, src, threads=threads)
    _assert_lane(dispatcher, native=companion == "present")
    support.check_contract(
        dispatcher.outputs[support.TABLE],
        legacy.outputs[support.TABLE],
        full.outputs[support.TABLE],
        src,
        string_output={"h", "r", "z"},
    )
    # A string-output column is `string` even when every chunk is all-null.
    out = dispatcher.outputs[support.TABLE]
    assert [out.schema.field(n).type for n in ("h", "r", "z")] == [pa.string()] * 3


PASSTHROUGH_TYPES = [t for t in matrix.BUILDERS if t != "time64_ns_nonaligned"]


@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize("typ", PASSTHROUGH_TYPES)
def test_every_admitted_passthrough_type_equals_the_source(
    typ: str, companion: str, tmp_path: Path
) -> None:
    arr = matrix.BUILDERS[typ]()
    src = support.table_of({**support.string_source(), "x": arr})
    cfg = support.make_cfg(
        [support.hash_col("h"), support.redact_col("r"), support.pass_col("x")],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    try:
        legacy = support.run_legacy(cfg, src)
    except Exception as exc:
        with pytest.raises(type(exc)):
            support.run_default(cfg, src)
        return
    dispatcher = support.run_default(cfg, src)
    _assert_lane(dispatcher, native=companion == "present")
    support.check_contract(
        dispatcher.outputs[support.TABLE],
        legacy.outputs[support.TABLE],
        None,
        src,
        string_output={"h", "r"},
    )


@pytest.mark.parametrize("companion", COMPANION_PARAMS)
@pytest.mark.parametrize(
    "variant", ["nonnullable_field", "field_metadata", "uint64_max", "unconfigured_column"]
)
def test_passthrough_field_shapes(variant: str, companion: str, tmp_path: Path) -> None:
    arr: pa.Array = pa.array([f"k{i}" for i in range(support.ROWS)])
    fields: dict[str, pa.Field] = {}
    configured = True
    if variant == "nonnullable_field":
        fields["x"] = pa.field("x", pa.string(), nullable=False)
    elif variant == "field_metadata":
        fields["x"] = pa.field("x", pa.string(), metadata={b"owner": b"b2"})
    elif variant == "uint64_max":
        arr = pa.array([2**64 - 1 - i for i in range(support.ROWS)], pa.uint64())
    else:
        configured = False
    src = support.table_of({**support.string_source(), "x": arr}, fields)
    cols = [support.hash_col("h"), support.redact_col("r")] + (
        [support.pass_col("x")] if configured else []
    )
    cfg = support.make_cfg(cols, path=support.write_source(src, tmp_path / "s.parquet"))
    dispatcher, legacy, full = _trio(cfg, src)
    # An unconfigured column under the `warn` policy (B8) takes the same route as a
    # configured passthrough column: native with the companion, the oracle without.
    _assert_lane(dispatcher, native=companion == "present")
    support.check_contract(
        dispatcher.outputs[support.TABLE],
        legacy.outputs[support.TABLE],
        full.outputs[support.TABLE],
        src,
        string_output={"h", "r"},
    )
    field = dispatcher.outputs[support.TABLE].schema.field("x")
    assert field.nullable is (variant != "nonnullable_field")
    assert (field.metadata is not None) is (variant == "field_metadata")


@pytest.mark.parametrize("companion", COMPANION_PARAMS)
def test_native_faker_over_an_all_null_source(companion: str, tmp_path: Path) -> None:
    """Guarantee 3 (b)'s one route-dependent cell: with the companion the native route
    builds a `string` array; on the oracle route (and on today's lane) the type is
    whatever the all-null chunks give (`null`). Values and null positions are equal."""
    columns, data = strategies.STRATEGY_FIXTURES["faker:deterministic_native"]
    src = pa.table({"val": pa.nulls(support.ROWS, pa.string())})
    cfg = support.make_cfg(columns, path=support.write_source(src, tmp_path / "s.parquet"))
    dispatcher, legacy, _full = _trio(cfg, src)
    out, legacy_out = dispatcher.outputs[support.TABLE], legacy.outputs[support.TABLE]
    assert (
        out.column("val").to_pylist()
        == legacy_out.column("val").to_pylist()
        == [None] * support.ROWS
    )
    if companion == "present":
        _assert_lane(dispatcher, native=True)
        assert out.schema.field("val").type == pa.string()
        support.check_contract(out, legacy_out, None, src, faker_native_all_null={"val"})
    else:
        _assert_lane(dispatcher, native=False, reason_prefix="index_extension_unavailable")
        # Equals the type test 0 records for today's lane and B1's oracle route.
        assert out.schema.field("val").type == legacy_out.schema.field("val").type
        support.check_contract(out, legacy_out, None, src, masked={"val"})


# ---------------------------------------------------------------------------
# Test 3 direct: `join_dispatcher_chunks`.
# ---------------------------------------------------------------------------


def _join() -> Any:
    from decoy_engine.execution._pipeline_auto_chunk import join_dispatcher_chunks

    return join_dispatcher_chunks


def test_join_keeps_equal_fields_with_nullability_and_field_metadata() -> None:
    field = pa.field("p", pa.string(), nullable=False, metadata={b"k": b"v"})
    chunks = [
        pa.Table.from_arrays([pa.array(["a", "b"])], schema=pa.schema([field])),
        pa.Table.from_arrays([pa.array(["c"])], schema=pa.schema([field])),
    ]
    joined = _join()(chunks, table="t")
    assert joined.schema.field("p").equals(field, check_metadata=True)
    assert joined.schema.metadata is None
    assert joined.column("p").to_pylist() == ["a", "b", "c"]
    assert joined.column("p").num_chunks == 1


def test_join_casts_a_null_typed_chunk_to_the_agreed_type() -> None:
    chunks = [
        pa.table({"a": pa.nulls(2)}),
        pa.table({"a": pa.array(["x", None])}),
    ]
    joined = _join()(chunks, table="t")
    assert joined.schema.field("a").type == pa.string()
    assert joined.column("a").to_pylist() == [None, None, "x", None]


def test_join_keeps_null_when_every_chunk_is_null() -> None:
    joined = _join()([pa.table({"a": pa.nulls(2)}), pa.table({"a": pa.nulls(1)})], table="t")
    assert joined.schema.field("a").type == pa.null()
    assert joined.num_rows == 3


def test_join_rejects_disagreeing_column_names_and_types() -> None:
    with pytest.raises(ExecutionError) as names:
        _join()([pa.table({"a": ["x"]}), pa.table({"b": ["x"]})], table="t")
    assert names.value.code == "chunked_schema_mismatch"
    with pytest.raises(ExecutionError) as types:
        _join()([pa.table({"a": ["x"]}), pa.table({"a": [1]})], table="t")
    assert types.value.code == "chunked_schema_mismatch"


def test_join_rejects_an_empty_chunk_list_without_an_index_error() -> None:
    with pytest.raises(ExecutionError) as empty:
        _join()([], table="accounts")
    assert empty.value.code == "chunked_schema_mismatch"
    assert "accounts" in str(empty.value)


def test_join_differs_from_concat_masked_chunks_on_a_nonnullable_field() -> None:
    """The reason the dispatcher lane has its own join: `concat_masked_chunks`
    rebuilds every field and turns a non-nullable passthrough field nullable."""
    field = pa.field("p", pa.string(), nullable=False)
    chunks = [pa.Table.from_arrays([pa.array(["a"])], schema=pa.schema([field]))] * 2
    assert _chunked.concat_masked_chunks(chunks, table="t").schema.field("p").nullable is True
    assert _join()(chunks, table="t").schema.field("p").nullable is False


# ---------------------------------------------------------------------------
# Test 3a: pandas-origin sources take the dispatcher lane.
# ---------------------------------------------------------------------------

N = support.ROWS


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"h": [f"u{i}@x.example" for i in range(N)], "r": [f"s{i}" for i in range(N)]}
    )


def _pandas_sources() -> dict[str, tuple[pd.DataFrame, bool, list[dict[str, Any]]]]:
    """name -> (frame, keep_index, extra column configs)."""
    cases: dict[str, tuple[pd.DataFrame, bool, list[dict[str, Any]]]] = {}
    p = [support.pass_col("p")]

    def frame(**cols: Any) -> pd.DataFrame:
        df = _frame()
        for k, v in cols.items():
            df[k] = v
        return df

    cases["StringDtype_passthrough"] = (
        frame(p=pd.array([f"k{i}" for i in range(N)], dtype="string")),
        False,
        p,
    )
    df = _frame()
    df["h"] = pd.array(list(df["h"]), dtype="string")
    cases["StringDtype_masked"] = (df, False, [])
    cases["Int8_nullfree"] = (frame(p=pd.array(list(range(N)), dtype="Int8")), False, p)
    cases["Int64_nullfree"] = (frame(p=pd.array(list(range(N)), dtype="Int64")), False, p)
    cases["boolean_nullfree"] = (
        frame(p=pd.array([i % 2 == 0 for i in range(N)], dtype="boolean")),
        False,
        p,
    )
    df = _frame()
    df.attrs = {"bench": "b2"}
    cases["DataFrame_attrs"] = (df, False, [])
    df = _frame()
    df.index = pd.RangeIndex(0, N, name="rid")
    cases["named_RangeIndex"] = (df, True, [])
    df = _frame()
    df.index = pd.RangeIndex(5, 5 + 3 * N, 3)
    cases["RangeIndex_start_and_step"] = (df, True, [])
    df = _frame()
    df.columns.name = "cols"
    cases["named_columns_index"] = (df, False, [])
    cases["datetimetz_passthrough"] = (
        frame(p=pd.to_datetime([i * 10**9 for i in range(N)]).tz_localize("UTC")),
        False,
        p,
    )
    return cases


def _as_parquet_and_memory(df: pd.DataFrame, keep_index: bool, tmp_path: Path) -> list[pa.Table]:
    table = pa.Table.from_pandas(df, preserve_index=keep_index)
    path = tmp_path / "written.parquet"
    pq.write_table(table, path)
    return [pq.read_table(path), table]


PANDAS_CASES = sorted(_pandas_sources())


@pytest.mark.parametrize("origin", ["parquet_read_back", "from_pandas"])
@pytest.mark.parametrize("name", PANDAS_CASES)
def test_pandas_origin_sources_take_the_dispatcher_lane(
    name: str, origin: str, tmp_path: Path
) -> None:
    df, keep_index, extra = _pandas_sources()[name]
    read_back, in_memory = _as_parquet_and_memory(df, keep_index, tmp_path)
    src = read_back if origin == "parquet_read_back" else in_memory
    cfg = support.make_cfg(
        [support.hash_col("h"), support.redact_col("r"), *extra],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    dispatcher, legacy, full = _trio(cfg, src)
    block = dispatcher.quality_metrics["auto_chunk"]
    assert block["mode"] == "chunked"
    assert block["lane"] == "dispatcher" and block["lane_reason"] is None
    out = dispatcher.outputs[support.TABLE]
    assert out.schema.metadata is None
    support.check_contract(
        out,
        legacy.outputs[support.TABLE],
        full.outputs[support.TABLE],
        src,
        string_output={"h", "r"},
    )


def test_pandas_metadata_naming_another_time_zone_keeps_the_field_zone(tmp_path: Path) -> None:
    """UTC `datetimetz` metadata on an `America/New_York` field: the output keeps the
    field's zone (test 0 records today's lane following the metadata instead)."""
    df = _frame()
    df["p"] = pd.to_datetime([i * 10**9 for i in range(N)]).tz_localize("UTC")
    table = pa.Table.from_pandas(df, preserve_index=False)
    ny = pa.timestamp("ns", "America/New_York")
    table = table.set_column(2, pa.field("p", ny), table.column("p").cast(ny))
    # Re-attach the pandas metadata, which still says UTC.
    table = table.replace_schema_metadata(
        pa.Table.from_pandas(df, preserve_index=False).schema.metadata
    )
    cfg = support.make_cfg(
        [support.hash_col("h"), support.redact_col("r"), support.pass_col("p")],
        path=support.write_source(table, tmp_path / "s.parquet"),
    )
    dispatcher, legacy, full = _trio(cfg, table)
    out = dispatcher.outputs[support.TABLE]
    assert out.schema.field("p").type == ny
    support.check_contract(
        out,
        legacy.outputs[support.TABLE],
        full.outputs[support.TABLE],
        table,
        string_output={"h", "r"},
    )


def test_int64_metadata_on_an_int32_field_and_a_pandas_key_beside_another_key(
    tmp_path: Path,
) -> None:
    df = _frame()
    df["p"] = np.arange(N, dtype=np.int64)
    good = pa.Table.from_pandas(df, preserve_index=False)
    table = good.set_column(2, pa.field("p", pa.int32()), good.column("p").cast(pa.int32()))
    table = table.replace_schema_metadata({**good.schema.metadata, b"other": b"key"})
    cfg = support.make_cfg(
        [support.hash_col("h"), support.redact_col("r"), support.pass_col("p")],
        path=support.write_source(table, tmp_path / "s.parquet"),
    )
    dispatcher, legacy, full = _trio(cfg, table)
    out = dispatcher.outputs[support.TABLE]
    assert out.schema.field("p").type == pa.int32()
    assert out.schema.metadata is None
    support.check_contract(
        out,
        legacy.outputs[support.TABLE],
        full.outputs[support.TABLE],
        table,
        string_output={"h", "r"},
    )


@pytest.mark.parametrize("origin", ["parquet_read_back", "from_pandas"])
def test_stored_non_range_index_column_is_absent_on_every_lane(origin: str, tmp_path: Path) -> None:
    """Guarantee 3 (a): a stored pandas index column, consumed as the index, is absent
    from today's output, from the dispatcher lane's and from the full frame's. It is
    not an output passthrough column (roadmap item PARQUET-INDEX will keep it)."""
    df = _frame()
    df.index = pd.Index([f"i{i}" for i in range(N)])
    table = pa.Table.from_pandas(df)
    assert "__index_level_0__" in table.column_names
    if origin == "parquet_read_back":
        path = tmp_path / "written.parquet"
        pq.write_table(table, path)
        table = pq.read_table(path)
    cfg = support.make_cfg(
        [support.hash_col("h"), support.redact_col("r")],
        path=support.write_source(table, tmp_path / "s.parquet"),
    )
    dispatcher, legacy, full = _trio(cfg, table)
    for result in (dispatcher, legacy, full):
        assert "__index_level_0__" not in result.outputs[support.TABLE].column_names
    assert (
        dispatcher.outputs[support.TABLE].column_names == legacy.outputs[support.TABLE].column_names
    )
    assert dispatcher.quality_metrics["auto_chunk"]["lane"] == "dispatcher"


def _raises_in_profile(
    cfg: dict[str, Any], src: pa.Table, monkeypatch: pytest.MonkeyPatch
) -> tuple[type[BaseException], str]:
    """The exception today's call raises without any B2 knob, then the same exception on
    both knob values, with every lane spied so entering one fails the test."""
    from decoy_engine.execution import run_pipeline

    spies = support.spy_lanes(monkeypatch)
    with pytest.raises(Exception) as base:
        run_pipeline(cfg, sources={support.TABLE: src}, **support.run_kwargs())
    for enabled in (True, False):
        with pytest.raises(base.type) as raised:
            run_pipeline(
                cfg,
                sources={support.TABLE: src},
                **support.run_kwargs(chunked_dispatcher_enabled=enabled),
            )
        assert str(raised.value) == str(base.value)
    assert not any(spies.values()), f"a lane was entered: {spies}"
    return base.type, str(base.value)


def test_malformed_pandas_metadata_fails_in_profile_source_and_never_enters_a_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = pa.Table.from_pandas(_frame(), preserve_index=False)
    src = src.replace_schema_metadata({b"pandas": b"{not json"})
    cfg = support.make_cfg(
        [support.hash_col("h"), support.redact_col("r")],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    exc_type, _message = _raises_in_profile(cfg, src, monkeypatch)
    assert exc_type is not TypeError, "the knob was rejected, not the source"


# ---------------------------------------------------------------------------
# Test 3b: time64[ns].
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("configured", [True, False])
def test_time64_ns_aligned_values_come_back_unchanged(configured: bool, tmp_path: Path) -> None:
    arr = pa.array([i * 1000 for i in range(support.ROWS)], pa.time64("ns"))
    src = support.table_of({**support.string_source(), "x": arr})
    cols = [support.hash_col("h"), support.redact_col("r")] + (
        [support.pass_col("x")] if configured else []
    )
    cfg = support.make_cfg(cols, path=support.write_source(src, tmp_path / "s.parquet"))
    dispatcher, legacy, _full = _trio(cfg, src)
    assert dispatcher.outputs[support.TABLE].schema.field("x").type == pa.time64("ns")
    assert legacy.outputs[support.TABLE].schema.field("x").type == pa.time64("us")
    support.check_contract(
        dispatcher.outputs[support.TABLE],
        legacy.outputs[support.TABLE],
        None,
        src,
        string_output={"h", "r"},
    )


@pytest.mark.parametrize("configured", [True, False])
def test_time64_ns_with_sub_microsecond_values_fails_in_profile_source(
    configured: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arr = pa.array([i * 1000 + 7 for i in range(support.ROWS)], pa.time64("ns"))
    src = support.table_of({**support.string_source(), "x": arr})
    cols = [support.hash_col("h"), support.redact_col("r")] + (
        [support.pass_col("x")] if configured else []
    )
    cfg = support.make_cfg(cols, path=support.write_source(src, tmp_path / "s.parquet"))
    exc_type, _message = _raises_in_profile(cfg, src, monkeypatch)
    assert exc_type is not TypeError
