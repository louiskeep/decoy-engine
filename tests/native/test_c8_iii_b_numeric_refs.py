"""C8-iii-b acceptance tests: auto-chunk a `when:` column whose predicate reads non-string columns.

Plan: docs/plans/2026-10-07-c8-iii-b-numeric-refs.md (rev 4), section 3. Every admitted case
asserts the route taken and checks the auto-chunk output contract: the `when` mask and the masked
column equal the WHOLE-FRAME run (not the chunked oracle), and every passthrough column, the
`when` references included, equals the SOURCE column exactly. Cases that must stay declined assert the existing planner reason.
"""

from __future__ import annotations

import copy
import datetime as dt
import decimal
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.execution import run_pipeline
from decoy_engine.execution._chunked_input import SourceFacts
from decoy_engine.execution.native._when_admission import planner_relaxed_when_columns
from decoy_engine.profile._readers import LazySource
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    key_provider,
    make_config,
    passthrough,
    redact,
    truncate,
)

N = 26
TARGET = [f"name-{i}" for i in range(N)]
DECLINE = "when_predicate_not_chunk_stable"


def _col(values: list[Any], typ: pa.DataType) -> pa.Array:
    return pa.array(values, typ)


def _table(refs: dict[str, pa.Array]) -> pa.Table:
    n = len(next(iter(refs.values())))
    return pa.table(
        {"s": pa.array(TARGET[:n] if n <= N else [f"name-{i}" for i in range(n)]), **refs}
    )


def _setup(
    tmp: Path, columns: list[dict[str, Any]], table: pa.Table, kind: str, **write: Any
) -> tuple[dict[str, Any], Any, pa.Table]:
    """(config, source handed to the auto run, table the whole-frame run reads)."""
    cfg = make_config(columns)
    path = str(tmp / "source.parquet")
    pq.write_table(table, path, **write)
    cfg["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    cfg["targets"][TABLE] = {"type": "file", "format": "parquet", "path": path + ".out"}
    if kind == "lazy":
        return cfg, LazySource(Path(path)), pq.read_table(path)
    return cfg, table, table


def _run(cfg: dict[str, Any], source: Any, **kw: Any) -> Any:
    return run_pipeline(
        copy.deepcopy(cfg),
        {TABLE: source},
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        **kw,
    )


def _auto(cfg: dict[str, Any], source: Any, chunk: int = 7) -> Any:
    return _run(cfg, source, auto_chunk_threshold_rows=5, chunk_size_rows=chunk)


def _full(cfg: dict[str, Any], table: pa.Table) -> Any:
    return _run(cfg, table, auto_chunk=False)


def _canon(table: pa.Table) -> list[tuple[str, str, list[str]]]:
    """Schema plus values with NaN made comparable, so equality is byte-level on the data."""
    return [
        (n, str(table.schema.field(n).type), [repr(v) for v in table.column(n).to_pylist()])
        for n in table.schema.names
    ]


def _selected(result: Any) -> list[bool]:
    """The row mask the run applied: which target values were masked."""
    out = result.outputs[TABLE].column("s").to_pylist()
    return [got != f"name-{i}" for i, got in enumerate(out)]


def _outcome(fn: Any) -> tuple[str, Any]:
    try:
        return "ok", fn()
    except Exception as exc:
        return "err", exc


def _assert_output_contract(got: pa.Table, full: pa.Table, source: pa.Table) -> None:
    """The auto-chunk output contract (dispatcher plan, guarantee 3): names, order and row
    count equal the whole-frame run; the masked column equals the whole frame in values,
    nulls and Arrow type; every passthrough column (each `when` reference included) equals
    the SOURCE column exactly, field metadata too, because the whole frame's pandas round
    trip is allowed to change it (NaN to null, large_string to string, date64 to date32)."""
    assert got.schema.names == full.schema.names
    assert got.num_rows == full.num_rows == source.num_rows
    for name in got.schema.names:
        if name == "s":
            assert _canon(got.select([name])) == _canon(full.select([name]))
            continue
        assert got.schema.field(name).equals(source.schema.field(name), check_metadata=True), name
        assert _canon(got.select([name])) == _canon(source.select([name])), name


def _assert_chunked_equals_full(
    cfg: dict[str, Any], source: Any, oracle: pa.Table, chunk: int = 7, grammar: bool = True
) -> list[bool] | None:
    """Assert the auto run equals the whole-frame run; the applied mask, or None when both
    runs raise the same `when` evaluation error (the planner must still have relaxed it)."""
    want = _outcome(lambda: _full(cfg, oracle))
    got = _outcome(lambda: _auto(cfg, source, chunk))
    if want[0] == "err":
        assert got[0] == "err", "the whole frame raised but the auto run did not"
        assert type(got[1]) is type(want[1])
        assert getattr(got[1], "code", None) == getattr(want[1], "code", None)
        # C8-iii-c: an out-of-grammar predicate is rejected at compile (when_outside_closed_grammar)
        # before it can reach eval; an in-grammar predicate that still fails at eval keeps
        # when_expression_error. Both the auto and whole-frame runs raise the same one.
        expect = "when_expression_error" if grammar else "when_outside_closed_grammar"
        assert getattr(got[1], "code", None) == expect
        relaxed = planner_relaxed_when_columns(cfg, None, TABLE, {TABLE: source}, {})
        assert relaxed == (frozenset({"s"}) if grammar else frozenset())
        return None
    assert got[0] == "ok", got[1]
    auto, full = got[1], want[1]
    block = auto.quality_metrics["auto_chunk"]
    assert block["mode"] == "chunked", block["reason"]
    assert full.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    route = auto.quality_metrics["chunked_route"]
    assert route["native_admitted"] is True, route["reroute_reason"]
    _assert_output_contract(auto.outputs[TABLE], full.outputs[TABLE], oracle)
    assert _selected(auto) == _selected(full)
    return _selected(auto)


def _cols(refs: dict[str, pa.Array], predicate: str) -> list[dict[str, Any]]:
    return [{**redact("s"), "when": predicate}, *(passthrough(k) for k in refs)]


# ---------------------------------------------------------------------------
# Test 1: relaxed and auto-chunked, byte-equal to the whole frame.
# ---------------------------------------------------------------------------

_BIG = 2**53
_NAN = float("nan")
_BASE = dt.datetime(2020, 1, 1)
_TS = [_BASE + dt.timedelta(hours=7 * i) for i in range(N)]
_DAYS = [dt.date(1970, 1, 1) + dt.timedelta(days=i) for i in range(N)]
_F = [(_NAN if i % 5 == 0 else None if i % 7 == 0 else 0.5 * (i % 4)) for i in range(N)]
_P = [("x" if i % 3 == 0 else "y" if i % 3 == 1 else None) for i in range(N)]


@dataclass(frozen=True)
class Case:
    refs: dict[str, pa.Array]
    predicate: str
    selection: str = "free"  # strict | none | all | error | free
    kinds: tuple[str, ...] = ("resident", "lazy")
    in_grammar: bool = True
    # A pandas-origin table keeps its schema metadata, so the run restores nullable dtypes.
    table: pa.Table | None = None
    dtypes: tuple[tuple[str, str], ...] = ()


def _cases() -> dict[str, Case]:
    ints = _col([_BIG + (i % 4) for i in range(N)], pa.int64())
    u64 = _col([2**63 + (i % 4) for i in range(N)], pa.uint64())
    f64 = _col(_F, pa.float64())
    f32 = _col([0.1 if i % 2 else 0.25 for i in range(N)], pa.float32())
    ts = _col(_TS, pa.timestamp("us"))
    ts_tz = _col(_TS, pa.timestamp("us", "UTC"))
    nullish_ts = _col([None if 7 <= i < 14 else _TS[i] for i in range(N)], pa.timestamp("us"))
    nullish_f = _col([None if 7 <= i < 14 else 0.5 * (i % 4) for i in range(N)], pa.float64())
    pd_frame = pd.DataFrame(
        {
            "s": TARGET,
            "i": pd.array(list(range(N)), dtype="Int64"),
            "u": pd.array(list(range(N)), dtype="UInt64"),
            "b": pd.array([i % 3 == 0 for i in range(N)], dtype="boolean"),
            "f": pd.array([None if i % 6 == 0 else i * 0.5 for i in range(N)], dtype="Float64"),
        }
    )
    pdt = pa.Table.from_pandas(pd_frame, preserve_index=False)

    def nullable(col: str, pred: str, dtype: str) -> Case:
        return Case(
            {col: pdt.column(col)},
            pred,
            "strict",
            table=pdt.select(["s", col]),
            dtypes=((col, dtype),),
        )

    cases: dict[str, Case] = {
        "int64_eq_above_2_53": Case({"n": ints}, f"n == {_BIG + 1}", "strict"),
        "int64_ne_above_2_53": Case({"n": ints}, f"n != {_BIG + 1}", "strict"),
        "int64_in_above_2_53": Case({"n": ints}, f"n in [{_BIG + 1}, {_BIG + 3}]", "strict"),
        "int64_gt": Case({"n": ints}, f"n > {_BIG + 1}", "strict"),
        # A literal above int64 is outside the closed grammar: the planner declines and both
        # runs raise the same error.
        "uint64_literal_above_int64": Case(
            {"u": u64}, f"u == {2**63 + 1}", "error", in_grammar=False
        ),
        "uint64_above_2_63": Case({"u": u64}, "u > 9"),
        "uint64_in_int64_range": Case({"u": u64}, "u != 5"),
        "float_eq": Case({"f": f64}, "f == 1.5", "strict"),
        "float_ne": Case({"f": f64}, "f != 1.5", "strict"),
        "float_in": Case({"f": f64}, "f in [0.5, 1.5]", "strict"),
        "date32_eq_false": Case({"d": _col(_DAYS, pa.date32())}, "d == '1970-01-01'", "none"),
        "date32_in_false": Case({"d": _col(_DAYS, pa.date32())}, "d in ['1970-01-01']", "none"),
        "date32_ne_true": Case({"d": _col(_DAYS, pa.date32())}, "d != '1970-01-01'", "all"),
        "date32_gt_error": Case({"d": _col(_DAYS, pa.date32())}, "d > '1970-01-05'", "error"),
        "date64_eq_false": Case(
            {"d": _col(_DAYS, pa.date64())}, "d == '1970-01-01'", "none", ("resident",)
        ),
        "date64_in_false": Case(
            {"d": _col(_DAYS, pa.date64())}, "d in ['1970-01-01']", "none", ("resident",)
        ),
        "date64_ne_true": Case(
            {"d": _col(_DAYS, pa.date64())}, "d != '1970-01-01'", "all", ("resident",)
        ),
        "date64_gt_error": Case(
            {"d": _col(_DAYS, pa.date64())}, "d > '1970-01-05'", "error", ("resident",)
        ),
        "date_plus_string_compound": Case(
            {"d": _col(_DAYS, pa.date32()), "p": _col(_P, pa.string())},
            "d != '1970-01-01' and p == 'x'",
            "strict",
        ),
        "tz_timestamp_ge": Case({"ts": ts_tz}, "ts >= '2020-01-03T00:00:00+00:00'", "strict"),
        "tz_timestamp_eq": Case({"ts": ts_tz}, "ts == '2020-01-01 07:00:00+00:00'", "strict"),
        "naive_timestamp_ge": Case({"ts": ts}, "ts >= '2020-01-03'", "strict"),
        "naive_timestamp_in": Case({"ts": ts}, "ts in ['2020-01-01 07:00:00']", "strict"),
        "bool_null_free": Case(
            {"b": _col([i % 3 == 0 for i in range(N)], pa.bool_())}, "b == True", "strict"
        ),
        "large_string": Case(
            {"l": _col(["x" if i % 2 else "y" for i in range(N)], pa.large_string())},
            "l == 'x'",
            "strict",
        ),
        "mixed_string_and_int": Case(
            {"p": _col(_P, pa.string()), "n": _col(list(range(N)), pa.int64())},
            "p == 'x' and n > 3",
            "strict",
        ),
        "float32_eq_literal": Case({"r": f32}, "r == 0.1"),
        "float32_in_literal": Case({"r": f32}, "r in [0.1]"),
        "float16": Case({"r": _col([0.5 * (i % 4) for i in range(N)], pa.float16())}, "r == 0.5"),
        "int8": Case({"r": _col([i % 8 for i in range(N)], pa.int8())}, "r > 3", "strict"),
        "int16": Case({"r": _col([i % 8 for i in range(N)], pa.int16())}, "r > 3", "strict"),
        "uint8": Case({"r": _col([i % 8 for i in range(N)], pa.uint8())}, "r > 3", "strict"),
        "nullable_Int64": nullable("i", "i > 3", "Int64"),
        "nullable_UInt64": nullable("u", "u > 3", "UInt64"),
        "nullable_boolean": nullable("b", "b == True", "boolean"),
        "nullable_Float64": nullable("f", "f > 3.0", "Float64"),
        "all_null_chunk_timestamp": Case({"ts": nullish_ts}, "ts >= '2020-01-03'", "strict"),
        "all_null_chunk_float": Case({"f": nullish_f}, "f != 0.5", "strict"),
    }
    return cases


_CASES = _cases()


@pytest.mark.parametrize("chunk", [7, 5])
@pytest.mark.parametrize("kind", ["resident", "lazy"])
@pytest.mark.parametrize("name", sorted(_CASES))
def test_a_relaxed_reference_auto_chunks_and_equals_the_whole_frame(
    name: str, kind: str, chunk: int, tmp_path: Path
) -> None:
    case = _CASES[name]
    if kind not in case.kinds:
        pytest.skip("date64 is stored as date32 in Parquet, so the lazy leg is the date32 case")
    table = case.table if case.table is not None else _table(case.refs)
    cfg, source, oracle = _setup(
        tmp_path, _cols(case.refs, case.predicate), table, kind, row_group_size=9
    )
    if case.dtypes:
        from decoy_engine.execution._fk_keys import to_pandas_fk_safe

        frame = to_pandas_fk_safe(oracle, ())
        assert tuple((c, str(frame[c].dtype)) for c, _ in case.dtypes) == case.dtypes
    mask = _assert_chunked_equals_full(cfg, source, oracle, chunk, grammar=case.in_grammar)
    if case.selection == "error":
        assert mask is None
        return
    assert mask is not None
    if case.selection == "strict":
        assert any(mask) and not all(mask)
    elif case.selection == "none":
        assert not any(mask)
    elif case.selection == "all":
        assert all(mask)


# Generated partitions: varied chunk sizes and null placements; mask and output both compared.

_PRED = {
    "float64": ["r != 1.5", "r == 1.5", "r in [0.5, 1.5]", "r > 0.5"],
    "timestamp": ["r >= '2020-01-03'", "r == '2020-01-01 07:00:00'", "r != '2020-01-02'"],
    "int64": [f"r == {_BIG + 1}", f"r != {_BIG + 2}", f"r in [{_BIG + 1}, {_BIG + 3}]"],
    "uint64": ["r > 1", "r != 5", "r == 9223372036854775807"],
    "bool": ["r == True", "r != True"],
}


@st.composite
def _partition(draw: Any) -> tuple[str, str, pa.Array, int]:
    kind = draw(st.sampled_from(sorted(_PRED)))
    rows = draw(st.integers(8, 40))
    chunk = draw(st.integers(3, 15))
    nulls = draw(st.lists(st.booleans(), min_size=rows, max_size=rows))
    pick = draw(st.lists(st.integers(0, 3), min_size=rows, max_size=rows))
    if kind == "float64":
        vals = [
            None if z else (_NAN if p == 3 else 0.5 * p) for z, p in zip(nulls, pick, strict=True)
        ]
        arr = pa.array(vals, pa.float64())
        return kind, draw(st.sampled_from(_PRED[kind])), arr, chunk
    if kind == "timestamp":
        vals = [
            None if z else _BASE + dt.timedelta(hours=7 * p * 5)
            for z, p in zip(nulls, pick, strict=True)
        ]
        return kind, draw(st.sampled_from(_PRED[kind])), pa.array(vals, pa.timestamp("us")), chunk
    if kind == "int64":
        arr = pa.array([_BIG + p for p in pick], pa.int64())
    elif kind == "uint64":
        arr = pa.array([2**63 + p for p in pick], pa.uint64())
    else:
        arr = pa.array([p % 2 == 0 for p in pick], pa.bool_())
    return kind, draw(st.sampled_from(_PRED[kind])), arr, chunk


@settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(_partition())
def test_generated_partitions_equal_the_whole_frame(case: tuple[str, str, pa.Array, int]) -> None:
    _kind, predicate, arr, chunk = case
    refs = {"r": arr}
    table = _table(refs)
    with tempfile.TemporaryDirectory() as tmp:
        cfg, source, oracle = _setup(Path(tmp), _cols(refs, predicate), table, "resident")
        _assert_chunked_equals_full(cfg, source, oracle, chunk)


# ---------------------------------------------------------------------------
# Test 2 and 3: still declined, with the existing reason naming exactly those columns.
# ---------------------------------------------------------------------------


def _declined(cfg: dict[str, Any], source: Any, oracle: pa.Table) -> Any:
    auto = _auto(cfg, source)
    block = auto.quality_metrics["auto_chunk"]
    assert block["mode"] == "full_frame"
    assert DECLINE in block["reason"]
    assert _canon(auto.outputs[TABLE]) == _canon(_full(cfg, oracle).outputs[TABLE])
    return block["reason"]


def _declined_columns(reason: str) -> list[str]:
    match = re.search(rf"{DECLINE}: column\(s\) (.+?) carry", reason)
    assert match, reason
    return match.group(1).split(", ")


def test_a_bool_reference_with_nulls_is_declined(tmp_path: Path) -> None:
    refs = {"b": _col([None if i % 4 == 0 else i % 3 == 0 for i in range(N)], pa.bool_())}
    cfg, source, oracle = _setup(tmp_path, _cols(refs, "b == True"), _table(refs), "resident")
    assert _declined_columns(_declined(cfg, source, oracle)) == ["s"]


@pytest.mark.parametrize(
    ("typ", "predicate", "values"),
    [
        (pa.bool_(), "b == True", [i % 3 == 0 for i in range(N)]),
        (pa.int64(), "b > 3", list(range(N))),
    ],
    ids=["bool", "int"],
)
def test_a_reference_without_a_known_null_count_is_declined(
    typ: pa.DataType, predicate: str, values: list[Any], tmp_path: Path
) -> None:
    refs = {"b": _col(values, typ)}
    cfg, source, oracle = _setup(
        tmp_path, _cols(refs, predicate), _table(refs), "lazy", write_statistics=False
    )
    assert LazySource(Path(cfg["sources"][TABLE]["path"])) is not None
    facts_less = SourceFacts(
        num_rows=N, schema=oracle.schema, null_counts={}, row_groups=1, max_row_group_rows=N
    )
    assert facts_less.null_count("b") is None
    assert planner_relaxed_when_columns(cfg, None, TABLE, {TABLE: source}, {TABLE: facts_less}) == (
        frozenset()
    )
    reason = _declined(cfg, source, oracle)
    assert _declined_columns(reason) == ["s"]


def test_a_dictionary_reference_is_declined(tmp_path: Path) -> None:
    refs = {"dv": pa.array(["x" if i % 2 else "y" for i in range(N)]).dictionary_encode()}
    cfg, source, oracle = _setup(tmp_path, _cols(refs, "dv == 'x'"), _table(refs), "resident")
    assert _declined_columns(_declined(cfg, source, oracle)) == ["s"]


@pytest.mark.parametrize(
    ("typ", "values"),
    [
        (pa.time32("s"), [dt.time(0, 0, i) for i in range(N)]),
        (pa.time64("us"), [dt.time(0, 0, i) for i in range(N)]),
    ],
    ids=["time32", "time64"],
)
def test_a_time_reference_is_declined(typ: pa.DataType, values: list[Any], tmp_path: Path) -> None:
    refs = {"tt": _col(values, typ)}
    cfg, source, oracle = _setup(tmp_path, _cols(refs, "tt == 1"), _table(refs), "resident")
    assert _declined_columns(_declined(cfg, source, oracle)) == ["s"]


def test_a_duration_reference_is_not_relaxed() -> None:
    refs = {"tt": _col(list(range(N)), pa.duration("ns"))}
    table = _table(refs)
    cfg = make_config(_cols(refs, "tt == 1"))
    assert planner_relaxed_when_columns(cfg, None, TABLE, {TABLE: table}, {}) == frozenset()


def test_an_out_of_grammar_predicate_is_rejected_at_compile(tmp_path: Path) -> None:
    # C8-iii-c: an out-of-grammar predicate (here `f.notnull()`, a method call) no longer reaches
    # the auto-chunk planner's decline path; it is rejected at compile for every caller. The
    # planner-decline path for an in-grammar but unstable reference is covered by
    # test_one_unstable_reference_declines_even_beside_a_stable_one.
    refs = {"f": _col(_F, pa.float64())}
    cfg, source, oracle = _setup(tmp_path, _cols(refs, "f.notnull()"), _table(refs), "resident")
    err = _outcome(lambda: _full(cfg, oracle))
    assert err[0] == "err"
    assert getattr(err[1], "code", None) == "when_outside_closed_grammar"


def test_a_numeric_reference_masked_earlier_is_declined(tmp_path: Path) -> None:
    refs = {"f": _col([0.5 * (i % 4) for i in range(N)], pa.float64())}
    columns = [{**redact("s"), "when": "f > 0.5"}, redact("f", redact_with=9.0)]
    cfg, source, oracle = _setup(tmp_path, columns, _table(refs), "resident")
    assert _declined_columns(_declined(cfg, source, oracle)) == ["s"]


def test_a_non_string_target_is_declined(tmp_path: Path) -> None:
    refs = {
        "f": _col([0.5 * (i % 4) for i in range(N)], pa.float64()),
        "t": _col(list(range(N)), pa.int64()),
    }
    columns = [{**redact("t", redact_with=0.5), "when": "f > 0.5"}, passthrough("f")]
    cfg, source, oracle = _setup(tmp_path, columns, _table(refs), "resident")
    assert _declined_columns(_declined(cfg, source, oracle)) == ["t"]


def test_the_reason_names_exactly_the_declined_columns(tmp_path: Path) -> None:
    refs = {
        "f": _col(_F, pa.float64()),
        "b": _col([None if i % 4 == 0 else i % 3 == 0 for i in range(N)], pa.bool_()),
        "l": _col(["x"] * N, pa.large_string()),
    }
    table = pa.table({"s": pa.array(TARGET), "t": pa.array(TARGET), "u": pa.array(TARGET), **refs})
    columns = [
        {**redact("s"), "when": "f > 0.5 and l == 'x'"},
        {**truncate("t"), "when": "b == True"},
        {**redact("u"), "when": "b == True"},
        *(passthrough(k) for k in refs),
    ]
    cfg, source, oracle = _setup(tmp_path, columns, table, "resident")
    assert _declined_columns(_declined(cfg, source, oracle)) == ["t", "u"]


# ---------------------------------------------------------------------------
# Allow-list and null-count unit pins on the relax rule itself.
# ---------------------------------------------------------------------------

_ALLOWED = [
    pa.string(),
    pa.large_string(),
    pa.float16(),
    pa.float32(),
    pa.float64(),
    pa.timestamp("s"),
    pa.timestamp("ms"),
    pa.timestamp("us"),
    pa.timestamp("ns"),
    pa.timestamp("us", "UTC"),
    pa.date32(),
    pa.date64(),
    pa.int8(),
    pa.int16(),
    pa.int32(),
    pa.int64(),
    pa.uint8(),
    pa.uint16(),
    pa.uint32(),
    pa.uint64(),
    pa.bool_(),
]
_DECLINED = [
    pa.time32("s"),
    pa.time64("us"),
    pa.duration("ns"),
    pa.decimal128(10, 2),
    pa.dictionary(pa.int8(), pa.string()),
    pa.list_(pa.int64()),
    pa.struct([("a", pa.int64())]),
    pa.binary(),
    pa.null(),
]


def _relaxed(typ: pa.DataType, *, null_count_known: bool = True, with_null: bool = False) -> bool:
    values = pa.nulls(4, typ) if with_null else None
    if values is None:
        values = _non_null(typ)
    table = pa.table({"s": pa.array(TARGET[:4]), "r": values})
    cfg = make_config([{**redact("s"), "when": "r == 1"}, passthrough("r")])
    facts: dict[str, SourceFacts] = {}
    if not null_count_known:
        facts = {TABLE: SourceFacts(num_rows=4, schema=table.schema, null_counts={})}
    out = planner_relaxed_when_columns(cfg, None, TABLE, {TABLE: table}, facts)
    return out == frozenset({"s"})


def _non_null(typ: pa.DataType) -> pa.Array:
    if pa.types.is_dictionary(typ):
        return pa.array(["a", "b", "a", "b"]).dictionary_encode()
    if pa.types.is_boolean(typ):
        return pa.array([True, False, True, False])
    if pa.types.is_string(typ) or pa.types.is_large_string(typ):
        return pa.array(["a", "b", "a", "b"], typ)
    if pa.types.is_binary(typ):
        return pa.array([b"a", b"b", b"a", b"b"])
    if pa.types.is_decimal(typ):
        return pa.array([decimal.Decimal(i) for i in range(4)], pa.decimal128(10, 2))
    if pa.types.is_list(typ):
        return pa.array([[1], [2], [3], [4]], typ)
    if pa.types.is_struct(typ):
        return pa.array([{"a": 1}] * 4, typ)
    if pa.types.is_null(typ):
        return pa.nulls(4)
    if pa.types.is_date64(typ):
        return pa.array([dt.date(2020, 1, 1 + i) for i in range(4)], typ)
    if pa.types.is_date32(typ):
        return pa.array([dt.date(2020, 1, 1 + i) for i in range(4)], typ)
    if pa.types.is_time(typ):
        return pa.array([dt.time(0, 0, i) for i in range(4)], typ)
    if pa.types.is_floating(typ):
        return pa.array([0.5, 1.0, 1.5, 2.0], pa.float64()).cast(typ)
    return pa.array([1, 2, 3, 4]).cast(typ)


@pytest.mark.parametrize("typ", _ALLOWED, ids=str)
def test_every_allow_listed_type_is_relaxed(typ: pa.DataType) -> None:
    assert _relaxed(typ)


@pytest.mark.parametrize("typ", _DECLINED, ids=str)
def test_every_other_type_is_not_relaxed(typ: pa.DataType) -> None:
    assert not _relaxed(typ)


_NULL_SENSITIVE = [
    pa.int8(),
    pa.int16(),
    pa.int32(),
    pa.int64(),
    pa.uint8(),
    pa.uint16(),
    pa.uint32(),
    pa.uint64(),
    pa.bool_(),
]


@pytest.mark.parametrize("typ", _NULL_SENSITIVE, ids=str)
def test_an_integer_or_bool_reference_needs_a_known_zero_null_count(typ: pa.DataType) -> None:
    assert not _relaxed(typ, with_null=True)
    assert not _relaxed(typ, null_count_known=False)


@pytest.mark.parametrize(
    "typ", [pa.float64(), pa.timestamp("us"), pa.string(), pa.date32()], ids=str
)
def test_nulls_and_unknown_counts_do_not_matter_for_stable_types(typ: pa.DataType) -> None:
    assert _relaxed(typ, with_null=True)
    assert _relaxed(typ, null_count_known=False)


def test_a_missing_reference_column_is_not_relaxed() -> None:
    table = pa.table({"s": pa.array(TARGET[:4])})
    cfg = make_config([{**redact("s"), "when": "ghost == 1"}])
    assert planner_relaxed_when_columns(cfg, None, TABLE, {TABLE: table}, {}) == frozenset()


def test_a_non_string_target_is_not_relaxed() -> None:
    table = pa.table({"t": pa.array([1, 2, 3, 4]), "r": pa.array([0.5, 1.0, 1.5, 2.0])})
    cfg = make_config([{**redact("t", redact_with=0.5), "when": "r > 1.0"}, passthrough("r")])
    assert planner_relaxed_when_columns(cfg, None, TABLE, {TABLE: table}, {}) == frozenset()


# ---------------------------------------------------------------------------
# Test 4: defense in depth. The relax rule checks null counts itself.
# ---------------------------------------------------------------------------


def test_an_int_reference_with_nulls_is_not_relaxed_even_with_the_table_gate_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _planner

    monkeypatch.setattr(_planner, "_runtime_source_rejections", lambda *a, **k: [])
    refs = {"n": _col([None if i % 4 == 0 else i for i in range(N)], pa.int64())}
    cfg, source, oracle = _setup(tmp_path, _cols(refs, "n > 3"), _table(refs), "resident")
    assert planner_relaxed_when_columns(cfg, None, TABLE, {TABLE: source}, {}) == frozenset()
    assert _declined_columns(_declined(cfg, source, oracle)) == ["s"]


# ---------------------------------------------------------------------------
# 2d: the multi-table split applies the rule per table.
# ---------------------------------------------------------------------------


def test_identically_named_references_are_judged_per_table(tmp_path: Path) -> None:
    from tests.unit.execution import _multi_table_support as mt

    rows = mt.BIG
    clean = pa.table(
        {
            "v": pa.array([f"a{i}" for i in range(rows)]),
            "r": pa.array(list(range(rows)), pa.int64()),
        }
    )
    nullish = pa.table(
        {
            "v": pa.array([f"b{i}" for i in range(rows)]),
            "r": pa.array([None if i % 5 == 0 else i for i in range(rows)], pa.int64()),
        }
    )
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "tbl_a": ([{**redact("v"), "when": "r > 3"}, passthrough("r")], clean),
            "tbl_b": ([{**redact("v"), "when": "r > 3"}, passthrough("r")], nullish),
        },
    )
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert mt.dispatched_tables(got) == ["tbl_a"]
    for name in ("tbl_a", "tbl_b"):
        assert _canon(got.outputs[name]) == _canon(off.outputs[name])


def test_one_unstable_reference_declines_even_beside_a_stable_one(tmp_path: Path) -> None:
    refs = {
        "f": _col(_F, pa.float64()),
        "b": _col([None if i % 4 == 0 else i % 3 == 0 for i in range(N)], pa.bool_()),
    }
    cfg, source, oracle = _setup(
        tmp_path, _cols(refs, "f > 0.5 and b == True"), _table(refs), "resident"
    )
    assert planner_relaxed_when_columns(cfg, None, TABLE, {TABLE: source}, {}) == frozenset()
    assert _declined_columns(_declined(cfg, source, oracle)) == ["s"]
