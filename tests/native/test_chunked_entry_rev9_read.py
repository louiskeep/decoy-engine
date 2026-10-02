"""B1 rev9 acceptance test 17: passthrough columns a `when:` predicate or a
sibling-reading strategy reads still go through pandas.

Such a column behaves exactly as on the public oracle, except that a value
pandas refuses raises the coded `chunked_passthrough_value_unrepresentable`
with the oracle's exception as its cause (rules R2 and R6).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.execution import _pandas_adapter
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.providers_v2 import get_default_registry
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
from tests.native._rev9_support import (
    BY_NAME,
    companion_missing,
    run_entry,
    run_public,
    same_column,
)
from tests.native.test_chunked_entry_rev7 import _DelegatingAdapter

REG = get_default_registry()
CODE = "chunked_passthrough_value_unrepresentable"
_T64 = BY_NAME["time64ns_unaligned"]
_S = pa.array(["a", "b", "c"])


def _when(name: str, expr: str, **cfg: Any) -> dict[str, Any]:
    return {**redact(name, **cfg), "when": expr}


def _chunk(**cols: Any) -> pa.Table:
    return pa.table({"s": _S, **cols})


def _read_lists(sink: list[Any]) -> list[Any]:
    return [r.quality_metrics["chunked_route"]["pandas_read_passthrough"] for r in sink]


def _expect_coded(info: Any, column: str, chunk_index: int) -> None:
    exc = info.value
    assert isinstance(exc, ExecutionError) and exc.code == CODE, repr(exc)
    assert exc.__cause__ is not None
    assert repr(TABLE) in exc.message and repr(column) in exc.message
    assert f"chunk {chunk_index}" in exc.message


def _public_error(config: dict[str, Any], chunks: list[pa.Table]) -> BaseException:
    with pytest.raises(Exception) as info:
        run_public(config, chunks)
    return info.value


def _same_exc(a: BaseException | None, b: BaseException) -> None:
    assert a is not None and type(a) is type(b) and str(a) == str(b)


# ---------------------------------------------------------------------------
# (a) a predicate names the column
# ---------------------------------------------------------------------------

_INTS = [pa.array([1, 5, 9]), pa.array([9, 1, 5]), pa.array([2, 6, 3])]

_CASES = {
    "unconfigured": ("x", "x > 4", False),
    "configured": ("x", "x > 4", True),
    "backtick": ("x", "`x` > 4", False),
    "backtick_configured": ("x", "`x` > 4", True),
    "needs_backticks": ("my col", "`my col` > 4", False),
    "needs_backticks_configured": ("my col", "`my col` > 4", True),
}


@pytest.mark.parametrize("forced", [False, True], ids=["plain", "companion_missing"])
@pytest.mark.parametrize("case", _CASES)
def test_predicate_read_column_matches_the_public_oracle(
    case: str, forced: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    name, expr, configured = _CASES[case]
    base = [_when("s", expr)] + ([passthrough(name)] if configured else [])
    config = make_config(base + ([hash_col("h")] if forced else []))
    chunks = [_chunk(**{name: arr}) for arr in _INTS]
    expected = run_public(make_config(base), chunks)
    if forced:
        chunks = [t.append_column("h", pa.array(["p", "q", "r"])) for t in chunks]
        with companion_missing(monkeypatch):
            out, sink, ev = run_entry(config, chunks)
    else:
        out, sink, ev = run_entry(config, chunks)
    assert ev[0].native_admitted is False
    assert ev[0].reroute_reason is not None
    assert ev[0].reroute_reason.startswith("when_predicate_not_native:s")
    for got, want in zip(out, expected, strict=True):
        assert got.column("s").to_pylist() == want.column("s").to_pylist()
        assert same_column(got.column(name), want.column(name))
    assert _read_lists(sink) == [[name]] * 3
    assert aggregate_chunked_route_evidence(sink)["pandas_read_passthrough"] == [name]


def _dict(values: list[Any]) -> pa.Array:
    return pa.DictionaryArray.from_arrays(pa.array([0, 1, None], pa.int32()), pa.array(values))


_UNICODE = {
    "café": ("café == 4", _dict([4, 5]), _dict([4, 4])),
    "Δ": ("Δ == 4", _dict([4, 5]), _dict([4, 4])),
    "名字": ("名字 == 'b'", _dict(["a", "b"]), _dict(["b", "b"])),
}


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "unconfigured"])
@pytest.mark.parametrize("name", list(_UNICODE))
def test_unicode_predicate_names_are_read(name: str, configured: bool) -> None:
    # A later chunk holds a dictionary pandas refuses. A missed read would carry the column,
    # complete, and differ from the public oracle, which raises there.
    expr, good, bad = _UNICODE[name]
    cols = [_when("s", expr)] + ([passthrough(name)] if configured else [])
    config = make_config(cols)
    chunks = [_chunk(**{name: good}), _chunk(**{name: bad})]
    oracle_exc = _public_error(config, chunks)
    sink: list[Any] = []
    gen = run_mask_chunked(
        config,
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        chunk_result_sink=sink,
    )
    assert next(gen).column("s").num_chunks >= 1
    with pytest.raises(ExecutionError) as info:
        next(gen)
    _expect_coded(info, name, 1)
    _same_exc(info.value.__cause__, oracle_exc)
    assert len(sink) == 1 and _read_lists(sink) == [[name]]


def test_predicate_the_tokenizer_rejects_reads_every_passthrough_column() -> None:
    cols = [_when("s", "`unterminated"), passthrough("x")]
    chunks = [_chunk(x=arr, y=arr) for arr in _INTS]
    config = make_config(cols)
    try:
        run_public(config, chunks)
    except Exception as exc:  # the pandas evaluation of the broken predicate
        oracle_error = type(exc)
    else:  # pragma: no cover - the predicate cannot evaluate
        oracle_error = None
    sink: list[Any] = []
    try:
        list(
            run_mask_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                chunk_result_sink=sink,
            )
        )
    except Exception as exc:
        assert type(exc) is oracle_error
    from decoy_engine.execution._chunked_carry import read_set

    assert read_set([_when("s", "`unterminated")], ["x", "y"], REG) == frozenset({"x", "y"})


def test_aggregate_rejects_differing_read_lists() -> None:
    def result(listed: Any) -> Any:
        evidence: dict[str, Any] = {
            "table": TABLE,
            "native_admitted": False,
            "reroute_reason": None,
            "columns": [],
        }
        if listed is not None:
            evidence["pandas_read_passthrough"] = listed
        return type("R", (), {"quality_metrics": {"chunked_route": evidence}})()

    assert aggregate_chunked_route_evidence([result(["x"]), result(["x"])])[
        "pandas_read_passthrough"
    ] == ["x"]
    assert aggregate_chunked_route_evidence([])["pandas_read_passthrough"] == []
    for pair in ([["x"], ["y"]], [["x"], None], [None, ["x"]], [[], ["x"]]):
        with pytest.raises(ExecutionError) as info:
            aggregate_chunked_route_evidence([result(pair[0]), result(pair[1])])
        assert info.value.code == "chunked_route_evidence_inconsistent"


# ---------------------------------------------------------------------------
# (b) a refused value in a read column
# ---------------------------------------------------------------------------


def _read_cfg(*read: str, configured: bool) -> dict[str, Any]:
    expr = " and ".join(f"{c}.notnull()" for c in read)
    return make_config([_when("s", expr)] + ([passthrough(c) for c in read] if configured else []))


@pytest.mark.parametrize("forced", [False, True], ids=["plain", "companion_missing"])
@pytest.mark.parametrize("configured", [True, False], ids=["configured", "unconfigured"])
def test_refused_value_in_chunk_two_is_coded(
    configured: bool, forced: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _read_cfg("x", configured=configured)
    chunks = [_chunk(x=_T64.good), _chunk(x=_T64.good), _chunk(x=_T64.bad)]
    oracle_exc = _public_error(config, chunks)
    assert type(oracle_exc).__name__ == "ArrowInvalid"
    sink: list[Any] = []
    kwargs = dict(
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        chunk_result_sink=sink,
    )
    if forced:
        config = make_config(
            [_when("s", "x.notnull()"), hash_col("h")] + ([passthrough("x")] if configured else [])
        )
        chunks = [t.append_column("h", pa.array(["p", "q", "r"])) for t in chunks]
        monkeypatch.setitem(__import__("sys").modules, "decoy_engine_native", None)
    gen = run_mask_chunked(config, chunks, **kwargs)
    first, second = next(gen), next(gen)
    assert first.num_rows == second.num_rows == 3
    with pytest.raises(ExecutionError) as info:
        next(gen)
    _expect_coded(info, "x", 2)
    _same_exc(info.value.__cause__, oracle_exc)
    assert len(sink) == 2


def test_refused_value_in_chunk_zero_is_coded_at_call_time() -> None:
    config = _read_cfg("x", configured=True)
    chunks = [_chunk(x=_T64.bad), _chunk(x=_T64.good)]
    oracle_exc = _public_error(config, chunks)
    with pytest.raises(ExecutionError) as info:
        run_mask_chunked(
            config, chunks, table=TABLE, engine_version=ENGINE_VERSION, key_provider=key_provider()
        )
    _expect_coded(info, "x", 0)
    _same_exc(info.value.__cause__, oracle_exc)


def test_two_read_columns_the_error_names_the_one_that_holds_the_value() -> None:
    config = _read_cfg("a", "b", configured=False)
    chunks = [
        _chunk(a=_T64.good, b=_T64.good),
        _chunk(a=_T64.good, b=_T64.good),
        _chunk(a=_T64.good, b=_T64.bad),
    ]
    gen = run_mask_chunked(
        config, chunks, table=TABLE, engine_version=ENGINE_VERSION, key_provider=key_provider()
    )
    next(gen)
    next(gen)
    with pytest.raises(ExecutionError) as info:
        next(gen)
    _expect_coded(info, "b", 2)


# ---------------------------------------------------------------------------
# (c) a column that is not read stays carried
# ---------------------------------------------------------------------------


def test_unread_passthrough_stays_carried_while_a_masked_column_is_read() -> None:
    config = make_config([_when("s", "y == 'q'"), redact("y")])
    chunks = [
        _chunk(y=pa.array(["q", "r", "q"]), x=_T64.good),
        _chunk(y=pa.array(["q", "r", "q"]), x=_T64.good),
        _chunk(y=pa.array(["q", "r", "q"]), x=_T64.bad),
    ]
    out, sink, _ev = run_entry(config, chunks)
    assert len(out) == 3
    assert same_column(out[2].column("x"), chunks[2].column("x"))
    assert _read_lists(sink) == [[]] * 3
    assert aggregate_chunked_route_evidence(sink)["pandas_read_passthrough"] == []


# ---------------------------------------------------------------------------
# (d) sibling-reading strategies
# ---------------------------------------------------------------------------


def _sibling_cases() -> dict[str, tuple[list[dict[str, Any]], list[pa.Table], str]]:
    dates = pa.array([f"19{60 + i}-03-0{1 + i}" for i in range(6)])
    ids = pa.array(["p1", "p1", "p2", "p2", "p3", "p3"])
    group_key = (
        [
            passthrough("g"),
            {
                "name": "k",
                "strategy": "group_key",
                "provider_config": {"group_by": "g"},
            },
        ],
        [pa.table({"g": ids, "k": pa.array(["a"] * 6)})],
        "g",
    )
    date_shift = (
        [
            passthrough("g"),
            {
                "name": "dob",
                "strategy": "date_shift",
                "namespace": "dob_ns",
                "provider_config": {"min_days": -30, "max_days": 30, "group_by": "g"},
            },
        ],
        [pa.table({"g": ids, "dob": dates})],
        "g",
    )
    windowed = (
        [
            passthrough("start"),
            {
                "name": "end",
                "strategy": "windowed_date",
                "provider_config": {
                    "anchor": "start",
                    "min_days": 0,
                    "max_days": 30,
                    "distribution": "uniform",
                },
            },
        ],
        [pa.table({"start": dates, "end": dates})],
        "start",
    )
    return {"group_key": group_key, "date_shift": date_shift, "windowed_date": windowed}


@pytest.mark.parametrize("case", ["group_key", "date_shift", "windowed_date"])
def test_sibling_reference_puts_the_passthrough_column_in_the_read_set(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns, chunks, ref = _sibling_cases()[case]
    config = make_config(columns)
    expected = run_public(config, chunks)
    seen: list[pa.DataType] = []
    real = _pandas_adapter.to_pandas_fk_safe

    def spy(table: pa.Table, fk: Any) -> Any:
        if ref in table.column_names:
            seen.append(table.schema.field(ref).type)
        return real(table, fk)

    monkeypatch.setattr(_pandas_adapter, "to_pandas_fk_safe", spy)
    out, sink, _ev = run_entry(config, chunks)
    assert seen and not any(pa.types.is_null(t) for t in seen), case
    assert _read_lists(sink) == [[ref]]
    for got, want in zip(out, expected, strict=True):
        assert got.to_pydict() == want.to_pydict()


# ---------------------------------------------------------------------------
# (e) a string literal that equals a column name over-approximates
# ---------------------------------------------------------------------------


def test_string_literal_equal_to_a_column_name_keeps_that_column_carried() -> None:
    config = make_config([_when("s", "s == 'x'")])
    chunks = [_chunk(x=pa.array([1, 2, 3])) for _ in range(2)]
    out, sink, ev = run_entry(config, chunks)
    assert _read_lists(sink) == [[]] * 2
    assert ev[0].native_admitted is False
    assert ev[0].reroute_reason == "when_predicate_not_native:s"
    expected = run_public(config, chunks)
    for got, src, want in zip(out, chunks, expected, strict=True):
        assert same_column(got.column("x"), src.column("x"))
        assert got.column("s").to_pylist() == want.column("s").to_pylist()


# ---------------------------------------------------------------------------
# (f) the read-set scan
# ---------------------------------------------------------------------------


def _scan(columns: list[dict[str, Any]], passthrough_names: list[str]) -> frozenset[str]:
    from decoy_engine.execution._chunked_carry import read_set

    return read_set(columns, passthrough_names, REG)


@pytest.mark.parametrize(
    ("expr", "names", "expected"),
    [
        ("x > 4", ["x", "y"], {"x"}),
        ("x > 4 and y < 2", ["x", "y", "z"], {"x", "y"}),
        ("café > 4", ["café", "cafe"], {"café"}),
        ("Δ > 4", ["Δ", "D"], {"Δ"}),
        ("名字 == 'b'", ["名字", "名"], {"名字"}),
        ("`my col` > 4", ["my col", "my", "col"], {"my col"}),
        ("`my col` > 4 and x > 1", ["my col", "x"], {"my col", "x"}),
        ("s == 'x'", ["x", "y"], set()),
        ('s == "café"', ["café"], set()),
        ("x == 'y'", ["x", "y"], {"x"}),
        ("`x` == 'y'", ["x", "y"], {"x"}),
        ("x.notnull()", ["x"], {"x"}),
        ("   ", ["x"], set()),
    ],
)
def test_read_set_scans_predicates(expr: str, names: list[str], expected: set[str]) -> None:
    assert _scan([{**redact("s"), "when": expr}], names) == frozenset(expected)


def test_read_set_tokenizer_failure_reads_every_passthrough_column() -> None:
    assert _scan([{**redact("s"), "when": "`unterminated"}], ["x", "y"]) == {"x", "y"}
    # one bad predicate among valid ones still poisons the table
    cols = [{**redact("s"), "when": "x > 1"}, {**redact("t"), "when": "`oops"}]
    assert _scan(cols, ["x", "y"]) == {"x", "y"}


def test_read_set_undecodable_string_literal_reads_every_passthrough_column() -> None:
    # A literal `ast.literal_eval` cannot evaluate (an f-string prefix is not a literal).
    assert _scan([{**redact("s"), "when": "s == f'{x}'"}], ["x", "y"]) == {"x", "y"}


def test_read_set_matches_sibling_reference_fields_only() -> None:
    cols = [
        {"name": "k", "strategy": "group_key", "provider_config": {"group_by": "g"}},
        {"name": "d", "strategy": "windowed_date", "provider_config": {"anchor": "a"}},
        {"name": "n", "strategy": "x", "provider_config": {"a": {"b": ["deep", 3]}, "c": ["lst"]}},
    ]
    assert _scan(cols, ["g", "a", "deep", "lst", "other"]) == {"g", "a"}
    # whole-string match only
    assert _scan(cols, ["gg", "g "]) == frozenset()


def test_read_set_ignores_names_that_are_not_column_references() -> None:
    cols = [
        {
            "name": "r",
            "strategy": "redact",
            "provider": "person_first_name",
            "namespace": "the_ns",
            "provider_config": {"label": "note", "nested": {"deep": ["lst"]}},
        },
    ]
    names = ["redact", "person_first_name", "the_ns", "note", "deep", "lst"]
    assert _scan(cols, names) == frozenset()


_READERS = {
    "date_shift_group_by": (
        {"name": "d", "strategy": "date_shift", "provider_config": {"group_by": "g"}},
        {"g"},
    ),
    "grouped_series_group_and_order": (
        {
            "name": "gs",
            "strategy": "grouped_series",
            "provider_config": {"group_by": "g", "order_by": "o"},
        },
        {"g", "o"},
    ),
    "coherent_with": (
        {"name": "c", "strategy": "redact", "coherent_with": ["g", "o"]},
        {"g", "o"},
    ),
    "derived_expression": (
        {"name": "v", "strategy": "derived", "provider_config": {"expression": "g + o * 2"}},
        {"g", "o"},
    ),
    "derived_aggregate_column": (
        {"name": "v", "strategy": "derived_aggregate", "provider_config": {"column": "g"}},
        {"g"},
    ),
    "joint_mask_key_and_columns": (
        {
            "name": "j",
            "strategy": "joint_mask",
            "provider_config": {"key_by": "g", "columns": ["o", "j"], "reference": "ref"},
        },
        {"g", "o"},
    ),
    "nested_child_group_by": (
        {
            "name": "n",
            "strategy": "nested",
            "provider_config": {"strategy": "group_key", "strategy_config": {"group_by": "g"}},
        },
        {"g"},
    ),
}


@pytest.mark.parametrize("case", sorted(_READERS))
def test_declared_sibling_fields_read_their_column(case: str) -> None:
    entry, expected = _READERS[case]
    assert _scan([entry], ["g", "o", "other"]) == frozenset(expected)


def test_unparsable_derived_expression_reads_every_passthrough_column() -> None:
    entry = {"name": "v", "strategy": "derived", "provider_config": {"expression": "g +"}}
    assert _scan([entry], ["g", "other"]) == {"g", "other"}


def _composite_entry(name: str, provider: str, strategy: str = "<composite>", **extra: Any) -> Any:
    return {
        "name": name,
        "strategy": strategy,
        "provider": provider,
        "deterministic": True,
        "namespace": "ns",
        **extra,
    }


_BUNDLE = [
    {"column": "a", "provider": "person_first_name"},
    {"column": "b", "provider": "person_last_name"},
    {"column": "c", "provider": "person_phone"},
]
_FIXED = {
    "composite_name_email": ("first_name", ["last_name", "email"]),
    "composite_city_state_zip": ("city", ["state", "zip"]),
    "composite_person": ("first_name", ["dob", "email", "last_name"]),
    "composite_address": ("city", ["state", "street_address", "zip"]),
    "composite_provider": ("provider_name", ["npi", "practice_address"]),
}


def _custom_pair(bundle: Any) -> list[dict[str, Any]]:
    cfg = {"provider_config": {"bundle": bundle}}
    return [
        _composite_entry("a", "composite_custom", coherent_with=["b"], **cfg),
        _composite_entry("b", "composite_custom", coherent_with=["a"], **cfg),
    ]


def test_composite_custom_bundle_output_is_read() -> None:
    from decoy_engine.execution._column_access import column_access

    cols = _custom_pair(_BUNDLE)
    assert _scan(cols, ["c"]) == {"c"}
    assert {"a", "b", "c"} <= column_access(cols[0], REG).writes
    assert _scan(cols, ["c", "d"]) == {"c"}


def test_composite_custom_bundle_without_the_column_reads_nothing() -> None:
    assert _scan(_custom_pair(_BUNDLE[:2]), ["c"]) == frozenset()


@pytest.mark.parametrize(
    "bundle", ["not-a-list", [{"column": ""}], [{"column": 3}], [{"nope": "c"}], [None]]
)
def test_malformed_composite_bundle_leaves_no_passthrough_column(bundle: Any) -> None:
    """Revision 4.5 (Design 12.8): an unresolvable bundle is an unknown WRITE, so no column
    may be carried or restored (`handler_written_columns` is None), rather than an unknown
    read that sends every passthrough column through pandas."""
    from decoy_engine.execution._chunked_carry import passthrough_columns
    from decoy_engine.execution._column_access import column_access, handler_written_columns

    cols = _custom_pair(bundle)
    assert column_access(cols[0], REG).writes_unknown is True
    assert handler_written_columns(cols, REG) is None
    config = {"tables": [{"name": "t", "columns": cols}]}
    assert passthrough_columns(config, table="t", names=["c", "d"], registry=REG) == []


@pytest.mark.parametrize("strategy", ["<composite>", "faker"])
@pytest.mark.parametrize("provider", sorted(_FIXED))
def test_lone_fixed_composite_reads_its_other_canonical_columns(
    provider: str, strategy: str
) -> None:
    from decoy_engine.execution._column_access import column_access

    own, others = _FIXED[provider]
    entry = _composite_entry(own, provider, strategy)
    assert column_access(entry, REG).writes >= set(others)
    assert _scan([entry], [*others, "unrelated"]) == frozenset(others)


@pytest.mark.parametrize("provider", sorted(_FIXED))
def test_lone_fixed_composite_with_when_keeps_the_full_declaration(provider: str) -> None:
    from decoy_engine.execution._column_access import column_access

    own, others = _FIXED[provider]
    entry = {**_composite_entry(own, provider), "when": f"{own} == 'never'"}
    assert column_access(entry, REG).writes >= set(others)
    assert _scan([entry], [*others, "unrelated"]) == frozenset(others)


def test_read_set_ignores_a_columns_own_name() -> None:
    assert _scan([{"name": "x", "strategy": "passthrough"}], ["x"]) == frozenset()
    cols = [{"name": "x", "strategy": "passthrough"}, {"name": "y", "strategy": "redact"}]
    assert _scan(cols, ["x"]) == frozenset()


def test_read_set_is_empty_without_passthrough_columns() -> None:
    assert _scan([{**redact("s"), "when": "x > 1"}], []) == frozenset()


# ---------------------------------------------------------------------------
# (g) the original exception propagates unwrapped
# ---------------------------------------------------------------------------


def _read_x_config(*extra: dict[str, Any]) -> dict[str, Any]:
    return make_config([_when("s", "x.notnull()"), *extra, passthrough("x")])


def test_masked_column_conversion_failure_is_not_wrapped() -> None:
    config = make_config([truncate("m"), _when("s", "x > 1"), passthrough("x")])
    ints = pa.array([1, 2, 3])
    chunks = [
        _chunk(m=_T64.good, x=ints),
        _chunk(m=_T64.bad, x=ints),
    ]
    oracle_exc = _public_error(config, chunks)
    gen = run_mask_chunked(
        config, chunks, table=TABLE, engine_version=ENGINE_VERSION, key_provider=key_provider()
    )
    next(gen)
    with pytest.raises(Exception) as info:
        next(gen)
    assert not isinstance(info.value, ExecutionError)
    _same_exc(info.value, oracle_exc)


def test_strategy_handler_failure_is_the_same_object(monkeypatch: pytest.MonkeyPatch) -> None:
    boom = ValueError("handler failed")

    def run(self: Any, *a: Any, **kw: Any) -> Any:
        raise boom

    monkeypatch.setattr(RedactHandler, "run", run)
    chunks = [_chunk(x=pa.array([1, 2, 3]))]
    gen = run_mask_chunked(
        _read_x_config(),
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
    )
    with pytest.raises(ValueError) as info:
        next(gen)
    assert info.value is boom


class _Raises:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def run(self, *a: Any, **kw: Any) -> Any:
        raise self.exc


def test_custom_adapter_failure_is_the_same_object() -> None:
    boom = ValueError("adapter failed")
    gen = run_mask_chunked(
        _read_x_config(),
        [_chunk(x=pa.array([1, 2, 3]))],
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        adapter=_Raises(boom),
    )
    with pytest.raises(ValueError) as info:
        next(gen)
    assert info.value is boom


def test_custom_adapter_converting_a_refused_passthrough_raises_raw() -> None:
    config = _read_x_config()
    chunks = [_chunk(x=_T64.good), _chunk(x=_T64.bad)]
    oracle_exc = _public_error(config, chunks)
    gen = run_mask_chunked(
        config,
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        adapter=_DelegatingAdapter(),
    )
    next(gen)
    with pytest.raises(Exception) as info:
        next(gen)
    assert not isinstance(info.value, ExecutionError)
    _same_exc(info.value, oracle_exc)


def test_stock_adapter_subclass_is_not_wrapped_either() -> None:
    class Sub(PandasExecutionAdapter):
        pass

    config = _read_x_config()
    chunks = [_chunk(x=_T64.good), _chunk(x=_T64.bad)]
    gen = run_mask_chunked(
        config,
        chunks,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        adapter=Sub(),
    )
    next(gen)
    with pytest.raises(Exception) as info:
        next(gen)
    assert type(info.value).__name__ == "ArrowInvalid"


# ---------------------------------------------------------------------------
# (h) nested columns fail in the profile walk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["list_int", "struct", "map"])
def test_read_nested_passthrough_is_coded_at_chunk_zero(name: str) -> None:
    shape = BY_NAME[name]
    config = make_config([_when("s", "tags.notnull()"), passthrough("tags")])
    chunks = [_chunk(tags=shape.good) for _ in range(2)]
    oracle_exc = _public_error(config, chunks)
    assert isinstance(oracle_exc, TypeError)
    with pytest.raises(ExecutionError) as info:
        run_mask_chunked(
            config, chunks, table=TABLE, engine_version=ENGINE_VERSION, key_provider=key_provider()
        )
    _expect_coded(info, "tags", 0)
    _same_exc(info.value.__cause__, oracle_exc)


# ---------------------------------------------------------------------------
# (i) profile-failure attribution follows source order
# ---------------------------------------------------------------------------

_LIST = BY_NAME["list_int"].good


def test_masked_profile_failure_next_to_a_valid_read_column_is_not_wrapped() -> None:
    config = make_config([redact("m"), _when("s", "x > 1"), passthrough("x")])
    chunks = [_chunk(m=_LIST, x=pa.array([1, 2, 3]))]
    oracle_exc = _public_error(config, chunks)
    with pytest.raises(TypeError) as info:
        run_mask_chunked(
            config, chunks, table=TABLE, engine_version=ENGINE_VERSION, key_provider=key_provider()
        )
    assert not isinstance(info.value, ExecutionError)
    _same_exc(info.value, oracle_exc)


def test_identical_profile_failures_are_attributed_in_source_order() -> None:
    # masked `m` and read passthrough `t` both fail the same way in the profile walk.
    config = make_config([redact("m"), _when("s", "t.notnull()"), passthrough("t")])
    masked_first = [pa.table({"s": _S, "m": _LIST, "t": _LIST})]
    with pytest.raises(TypeError) as info:
        run_mask_chunked(
            config,
            masked_first,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    assert not isinstance(info.value, ExecutionError)
    read_first = [pa.table({"s": _S, "t": _LIST, "m": _LIST})]
    with pytest.raises(ExecutionError) as coded:
        run_mask_chunked(
            config,
            read_first,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
        )
    _expect_coded(coded, "t", 0)
