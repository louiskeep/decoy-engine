"""A generate formula reads each referenced column with the column's own type.

Covers the type a formula binds (sequence, int and float faker, categorical, constant, reference),
the row rule for missing cells, and the coded, value-free failure of a formula that raises.
"""

from __future__ import annotations

import logging
from typing import Any, get_args

import pyarrow as pa
import pytest

from decoy_engine.config._tables import GenerateColumnConfig
from decoy_engine.generation.pool import GenerationError
from tests.unit._dps_helpers import compile_and_generate

SENTINEL = "SENTINEL-X1"


def cfg(cols: list[dict[str, Any]], rows: int = 5, extra_tables: list[dict] | None = None) -> dict:
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {},
        "tables": [
            {"name": "t", "row_count": rows, "generate_columns": cols},
            *(extra_tables or []),
        ],
    }


def seq(name: str = "a", **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "sequence", "start": 1, **extra}


def fml(name: str, expr: str, refs: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    col: dict[str, Any] = {"name": name, "type": "formula", "formula": expr, **extra}
    if refs is not None:
        col["references"] = refs
    return col


def run(config: dict) -> pa.Table:
    return compile_and_generate(config)["t"]


def values(config: dict, column: str) -> list[Any]:
    return run(config).column(column).to_pylist()


# AT-2 --------------------------------------------------------------------------------------


def test_sequence_is_a_number_to_a_formula() -> None:
    table = run(cfg([seq(), fml("m", "a * 2", ["a"]), fml("e", "a == 1", ["a"])]))
    assert table.column("m").to_pylist() == [2, 4, 6, 8, 10]
    assert table.column("e").to_pylist() == [True, False, False, False, False]
    assert table.schema.field("a").type == pa.int64()


@pytest.mark.parametrize("kind", ["pyint", "pyfloat"])
def test_numeric_faker_is_a_number_to_a_formula(kind: str) -> None:
    cols = [
        {"name": "x", "type": "faker", "faker_type": kind},
        fml("d", "x * 2", ["x"]),
    ]
    table = run(cfg(cols))
    x, d = table.column("x").to_pylist(), table.column("d").to_pylist()
    assert d == [v * 2 for v in x]
    assert table.schema.field("x").type == (pa.int64() if kind == "pyint" else pa.float64())


# AT-3 --------------------------------------------------------------------------------------

_EXAMPLES: dict[str, list[dict[str, Any]]] = {
    "sequence": [seq("col")],
    "faker": [{"name": "col", "type": "faker", "faker_type": "pyint"}],
    "categorical": [{"name": "col", "type": "categorical", "categories": [1, 2, 3]}],
    "constant": [{"name": "col", "type": "constant", "value": 7}],
    "distribution": [
        {"name": "col", "type": "distribution", "family": "uniform", "low": 10, "high": 100}
    ],
}
_EXTRA_EXAMPLES: dict[str, list[dict[str, Any]]] = {
    "faker_float": [{"name": "col", "type": "faker", "faker_type": "pyfloat"}],
    "categorical_float": [{"name": "col", "type": "categorical", "categories": [1.5, 2.5]}],
    "categorical_text": [{"name": "col", "type": "categorical", "categories": ["u", "v"]}],
    "grouped_series": [
        {"name": "grp", "type": "categorical", "categories": ["g1", "g2"]},
        seq("ord"),
        {
            "name": "col",
            "type": "grouped_series",
            "generator": "cumcount",
            "group_by": "grp",
            "order_by": "ord",
            "start": 1,
            "step": 1,
        },
    ],
}
_EXEMPT = {
    "formula": "a formula column is not read as a typed candidate",
    "derived": "its type follows its expression and keeps the binding as before",
    "derived_aggregate": "its type follows its aggregate and keeps the binding as before",
    "group_key": "text labels",
    "windowed_date": "dates are text on every route",
    "statistical": "snapshot-driven, keeps the binding as before",
    "lookup": "catalog values, keeps the binding as before",
    "reference": "covered by its own cell below",
}


def test_every_generate_type_has_an_example_or_a_reason() -> None:
    types = set(get_args(GenerateColumnConfig.model_fields["type"].annotation))
    covered = set(_EXAMPLES) | {"grouped_series"} | set(_EXEMPT)
    assert types <= covered, sorted(types - covered)


def _check_fidelity(cols: list[dict[str, Any]], null_p: float, tables: list[dict] | None = None):
    cols = [dict(c, null_probability=null_p) if c["name"] == "col" else c for c in cols]
    cols.append(fml("out", "col", ["col"]))
    table = compile_and_generate(cfg(cols, rows=20, extra_tables=tables))["t"]
    src, out = table.column("col"), table.column("out").to_pylist()
    for s, o in zip(src.to_pylist(), out, strict=True):
        if s is None:
            continue
        assert o == s
        assert type(o) is type(s)


# The nullable grouped-series cell is exempt: it is not a candidate type, so a missing cell binds "".
_CELLS = [
    (name, null_p)
    for name in sorted({**_EXAMPLES, **_EXTRA_EXAMPLES})
    for null_p in (0.0, 0.5)
    if (name, null_p) != ("grouped_series", 0.5)
]


@pytest.mark.parametrize(("name", "null_p"), _CELLS)
def test_formula_reads_the_source_column_type(name: str, null_p: float) -> None:
    _check_fidelity({**_EXAMPLES, **_EXTRA_EXAMPLES}[name], null_p)


@pytest.mark.parametrize("null_p", [0.0, 0.5])
def test_formula_reads_a_reference_to_a_sequence_parent(null_p: float) -> None:
    parent = {"name": "p", "row_count": 5, "generate_columns": [seq("id")]}
    child = {
        "name": "t",
        "row_count": 20,
        "generate_columns": [
            {
                "name": "col",
                "type": "reference",
                "reference_table": "p",
                "reference_column": "id",
                "null_probability": null_p,
            },
            fml("out", "col", ["col"]),
        ],
    }
    config = {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {},
        "tables": [parent, child],
    }
    table = compile_and_generate(config)["t"]
    for s, o in zip(table.column("col").to_pylist(), table.column("out").to_pylist(), strict=True):
        if s is not None:
            assert o == s
            assert type(o) is int
    assert table.schema.field("col").type == pa.int64()


# AT-4 --------------------------------------------------------------------------------------


def test_null_number_reference_gives_null_and_other_rows_do_not_move() -> None:
    nullable = run(cfg([seq(null_probability=0.5), fml("f", "'ORD-' + str(a)", ["a"])], rows=40))
    plain = run(cfg([seq(), fml("f", "'ORD-' + str(a)", ["a"])], rows=40))
    for i, (a, f) in enumerate(
        zip(nullable.column("a").to_pylist(), nullable.column("f").to_pylist(), strict=True)
    ):
        if a is None:
            assert f is None
        else:
            assert f == f"ORD-{a}" == plain.column("f").to_pylist()[i]


def test_skipped_null_rows_do_not_move_the_draws_of_other_rows() -> None:
    expr = "str(a) + '-' + str(randint(0, 10**9))"
    nullable = values(cfg([seq(null_probability=0.5), fml("f", expr, ["a"])], rows=40), "f")
    plain = values(cfg([seq(), fml("f", expr, ["a"])], rows=40), "f")
    assert any(v is None for v in nullable)
    for n, p in zip(nullable, plain, strict=True):
        assert n is None or n == p


def test_categorical_int_with_nulls_does_not_become_float_text() -> None:
    cols = [
        {"name": "c", "type": "categorical", "categories": [1, 2, 3], "null_probability": 0.5},
        fml("s", "str(c)", ["c"]),
    ]
    table = run(cfg(cols, rows=30))
    for c, s in zip(table.column("c").to_pylist(), table.column("s").to_pylist(), strict=True):
        assert s == (None if c is None else str(int(c)))


# AT-5 / AT-5b / AT-6 -------------------------------------------------------------------------


def test_failing_row_fails_the_run_with_a_value_free_coded_error(monkeypatch) -> None:
    import decoy_engine.generators._formula as mod

    calls: list[int] = []
    real = mod.safe_eval

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "safe_eval", spy)
    with pytest.raises(GenerationError) as info:
        run(cfg([seq(), fml("f", "1 / (a - 3)", ["a"])]))
    err = info.value
    assert err.code == "generate_formula_evaluation_failed"
    assert (
        err.message
        == "formula column evaluation failed at table 't', column 'f', row 2: division by zero"
    )
    assert err.__cause__ is None and err.__context__ is None
    assert len(calls) == 3  # rows 0, 1, 2; nothing after the first failure


def test_error_message_never_holds_a_cell_value(caplog) -> None:
    cols = [
        {"name": "code", "type": "categorical", "categories": [SENTINEL]},
        fml("f", "int(code)", ["code"]),
    ]
    with caplog.at_level(logging.DEBUG), pytest.raises(GenerationError) as info:
        run(cfg(cols))
    assert info.value.code == "generate_formula_evaluation_failed"
    assert SENTINEL not in str(info.value)
    assert SENTINEL not in info.value.message
    assert SENTINEL not in caplog.text
    assert info.value.__cause__ is None and info.value.__context__ is None


@pytest.mark.parametrize("expr", ["randint(5, 1)", "random.randint(1, 100)"])
def test_inline_formula_that_raises_fails_the_run(expr: str) -> None:
    with pytest.raises(GenerationError) as info:
        run(cfg([fml("f", expr)]))
    assert info.value.code == "generate_formula_evaluation_failed"
    assert info.value.__context__ is None


# AT-7 (control) ------------------------------------------------------------------------------


def test_text_reference_with_nulls_stays_null_on_a_failed_row() -> None:
    cols = [
        {"name": "code", "type": "categorical", "categories": ["1", "2"], "null_probability": 0.5},
        fml("n", "int(code)", ["code"]),
    ]
    table = run(cfg(cols, rows=30))
    for code, n in zip(
        table.column("code").to_pylist(), table.column("n").to_pylist(), strict=True
    ):
        assert n == (None if code is None else int(code))


# AT-9 ----------------------------------------------------------------------------------------


def test_sequence_read_by_derived_and_formula_runs() -> None:
    cols = [
        seq(),
        {"name": "d", "type": "derived", "expression": "a + 1"},
        fml("f", "str(a)", ["a"]),
    ]
    table = run(cfg(cols))
    assert table.column("d").to_pylist() == [2, 3, 4, 5, 6]
    assert table.schema.field("d").type == pa.int64()
    assert table.column("f").to_pylist() == ["1", "2", "3", "4", "5"]


# AT-12 ---------------------------------------------------------------------------------------


def test_co_reader_demotes_output_but_the_formula_still_sees_a_number() -> None:
    cols = [
        seq(),
        {"name": "g", "type": "group_key", "group_by": "a"},
        fml("m", "a * 2", ["a"]),
    ]
    table = run(cfg(cols))
    assert table.column("m").to_pylist() == [2, 4, 6, 8, 10]
    assert table.schema.field("a").type == pa.string()


def test_co_reader_float_faker_formula_equals_twice_the_output_text() -> None:
    cols = [
        {"name": "amount", "type": "faker", "faker_type": "pyfloat"},
        {"name": "tot", "type": "derived_aggregate", "op": "sum", "column": "amount"},
        fml("m", "amount * 2", ["amount"]),
    ]
    table = run(cfg(cols))
    assert table.schema.field("amount").type == pa.string()
    texts, m = table.column("amount").to_pylist(), table.column("m").to_pylist()
    assert m == [float(t) * 2 for t in texts]
