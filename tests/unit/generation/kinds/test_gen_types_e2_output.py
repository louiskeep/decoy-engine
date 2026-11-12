"""GEN-TYPES E2: AT-7 type and text on every route, AT-8 choke point, AT-9 reference, AT-15 text, AT-17 values.

Mutants named in the plan: `round_to_scale` with ROUND_HALF_UP (AT-7, see the catalog tests),
the cast call removed from `arrow_table` or from `_table_batches` (AT-8 below patches each site to
a no-op), `output_type` dropped from `_EXCLUDED_FROM_FINGERPRINT` (AT-17 and the config tests).
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pytest

from decoy_engine import declared_casting as dc
from decoy_engine.generation import kinds
from decoy_engine.generation.pinned import compile_pinned_plan, execute_pinned
from tests.parity._fr0_support import run_resident, run_streamed
from tests.unit.generation.kinds._support import cfg, generate, one, table

D = Decimal


def _spec_type(output: str) -> pa.DataType:
    from decoy_engine.config import parse_output_type

    kind, scale = parse_output_type(output)  # type: ignore[misc]
    return dc.arrow_type_for_spec(dc.validate_format_spec(dc.generated_value_spec(kind, scale)))


def _col(name: str, kind: str, output: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": kind, "output_type": output, **extra}


def _g() -> dict[str, Any]:
    return {"name": "g", "type": "categorical", "categories": ["a", "b"]}


def _o() -> dict[str, Any]:
    return {"name": "o", "type": "sequence", "start": 1}


def _v() -> dict[str, Any]:
    return {"name": "v", "type": "sequence", "start": 10}


def _five() -> dict[str, Any]:
    return {"name": "v", "type": "constant", "value": 5}


# (id, helper columns before x, x column, route set, first value, csv text of the first row)
# route set: "s" = stream core admits it, "p" = pinned mode admits it. Resident whole-table is always run.
CELLS: list[tuple[str, list[dict[str, Any]], dict[str, Any], str, Any, str]] = [
    ("const-int", [], _col("x", "constant", "int", value=1001), "sp", 1001, "1001"),
    ("const-dec", [], _col("x", "constant", "decimal(2)", value=12.3), "sp", D("12.30"), "12.30"),
    ("const-dec0", [], _col("x", "constant", "decimal(0)", value=12.5), "sp", D("12"), "12"),
    (
        "const-date",
        [],
        _col("x", "constant", "date", value="2026-01-02"),
        "sp",
        date(2026, 1, 2),
        "2026-01-02",
    ),
    ("const-bool", [], _col("x", "constant", "bool", value=True), "sp", True, "true"),
    ("const-bool-text", [], _col("x", "constant", "bool", value="FALSE"), "sp", False, "false"),
    ("const-str-int", [], _col("x", "constant", "int", value="42"), "sp", 42, "42"),
    ("cat-int", [], _col("x", "categorical", "int", categories=[7]), "sp", 7, "7"),
    (
        "cat-dec",
        [],
        _col("x", "categorical", "decimal(3)", categories=[1.5]),
        "sp",
        D("1.500"),
        "1.500",
    ),
    (
        "cat-date",
        [],
        _col("x", "categorical", "date", categories=["2026-03-04"]),
        "sp",
        date(2026, 3, 4),
        "2026-03-04",
    ),
    ("cat-bool", [], _col("x", "categorical", "bool", categories=[False]), "sp", False, "false"),
    (
        "dist-dec",
        [],
        _col("x", "distribution", "decimal(2)", family="uniform", low=1, high=2),
        "sp",
        None,
        "",
    ),
    (
        "dist-dec0",
        [],
        _col("x", "distribution", "decimal(0)", family="uniform", low=5, high=6),
        "sp",
        None,
        "",
    ),
    ("seq-int", [], _col("x", "sequence", "int", start=1001), "sp", 1001, "1001"),
    ("seq-dec", [], _col("x", "sequence", "decimal(2)", start=1001), "sp", D("1001.00"), "1001.00"),
    ("faker-int", [], _col("x", "faker", "int", faker_type="random_int"), "s", None, ""),
    ("faker-dec", [], _col("x", "faker", "decimal(2)", faker_type="random_int"), "s", None, ""),
    ("faker-date", [], _col("x", "faker", "date", faker_type="date"), "s", None, ""),
    ("faker-bool", [], _col("x", "faker", "bool", faker_type="pybool"), "s", None, ""),
    (
        "windowed",
        [{"name": "a", "type": "constant", "value": "2026-01-01"}],
        _col("x", "windowed_date", "date", anchor="a", max_days=3),
        "sp",
        None,
        "",
    ),
    ("derived-int", [_v()], _col("x", "derived", "int", expression="v * 2"), "p", 20, "20"),
    (
        "derived-dec",
        [_v()],
        _col("x", "derived", "decimal(2)", expression="v * 2"),
        "p",
        D("20.00"),
        "20.00",
    ),
    ("derived-bool", [_v()], _col("x", "derived", "bool", expression="v > 5"), "p", True, "true"),
    ("formula-int", [], _col("x", "formula", "int", formula="7"), "", 7, "7"),
    ("formula-dec", [], _col("x", "formula", "decimal(2)", formula="7"), "", D("7.00"), "7.00"),
    (
        "series-int",
        [_g(), _o()],
        _col("x", "grouped_series", "int", group_by="g", order_by="o"),
        "p",
        None,
        "",
    ),
    (
        "series-dec",
        [_g(), _o()],
        _col("x", "grouped_series", "decimal(1)", group_by="g", order_by="o"),
        "p",
        None,
        "",
    ),
    (
        "agg-int",
        [_five()],
        _col("x", "derived_aggregate", "int", op="sum", column="v"),
        "p",
        60,
        "60",
    ),
    (
        "agg-dec",
        [_five()],
        _col("x", "derived_aggregate", "decimal(2)", op="sum", column="v"),
        "p",
        D("60.00"),
        "60.00",
    ),
]
IDS = [c[0] for c in CELLS]
ROWS = 12


def _config(helpers: list[dict[str, Any]], x: dict[str, Any], rows: int = ROWS) -> dict[str, Any]:
    return cfg([table("t", rows, [*helpers, x])])


def _legacy(helpers: list[dict[str, Any]], x: dict[str, Any]) -> dict[str, Any]:
    """A `formula` sibling forces the whole-table route for the same column."""
    return _config([*helpers], x) | {
        "tables": [
            table("t", ROWS, [*helpers, x, {"name": "zz", "type": "formula", "formula": "1"}])
        ]
    }


def _csv_text(tbl: pa.Table, name: str = "x") -> list[str]:
    sink = io.BytesIO()
    pacsv.write_csv(tbl.select([name]), sink, pacsv.WriteOptions(include_header=False))
    return sink.getvalue().decode().splitlines()


def _check(tbl: pa.Table, output: str, first: Any, text: str) -> None:
    field = tbl.schema.field("x")
    assert field.type == _spec_type(output)
    values = tbl.column("x").to_pylist()
    assert len(values) == ROWS
    assert all(v is not None for v in values)
    if first is not None:
        assert values[0] == first
        assert type(values[0]) is type(first)
        assert _csv_text(tbl)[0].strip('"') == text


@pytest.mark.parametrize(("label", "helpers", "x", "routes", "first", "text"), CELLS, ids=IDS)
def test_resident_whole_table_route_emits_the_declared_type(
    label: str, helpers: list[dict[str, Any]], x: dict[str, Any], routes: str, first: Any, text: str
) -> None:
    _check(generate(_legacy(helpers, x))["t"], x["output_type"], first, text)


@pytest.mark.parametrize(("label", "helpers", "x", "routes", "first", "text"), CELLS, ids=IDS)
def test_stream_core_route_emits_the_declared_type(
    label: str, helpers: list[dict[str, Any]], x: dict[str, Any], routes: str, first: Any, text: str
) -> None:
    if "s" not in routes:
        pytest.skip("the stream core declines this type")
    _check(generate(_config(helpers, x))["t"], x["output_type"], first, text)
    # The partitioned resident core cuts batches mid-table; the cast must not care.
    from tests.parity._fr0_support import partition_from_cuts

    cut = run_resident(_config(helpers, x), partition_from_cuts({"t": [1, 5, 5]}))["t"]
    assert cut.equals(generate(_config(helpers, x))["t"])


@pytest.mark.parametrize(("label", "helpers", "x", "routes", "first", "text"), CELLS, ids=IDS)
def test_streamed_sink_route_emits_the_declared_type(
    label: str,
    helpers: list[dict[str, Any]],
    x: dict[str, Any],
    routes: str,
    first: Any,
    text: str,
    tmp_path: Path,
) -> None:
    if "s" not in routes:
        pytest.skip("the stream core declines this type")
    _, tables = run_streamed(_config(helpers, x), tmp_path)
    resident = generate(_config(helpers, x))["t"]
    got = tables["t"]
    assert (
        got.schema.field("x").type
        == resident.schema.field("x").type
        == _spec_type(x["output_type"])
    )
    assert got.column("x").to_pylist() == resident.column("x").to_pylist()


@pytest.mark.parametrize(("label", "helpers", "x", "routes", "first", "text"), CELLS, ids=IDS)
def test_pinned_route_agrees_with_the_resident_route(
    label: str, helpers: list[dict[str, Any]], x: dict[str, Any], routes: str, first: Any, text: str
) -> None:
    if "p" not in routes:
        pytest.skip("pinned mode does not admit this type")
    config = _config(helpers, x)
    config["global_settings"]["reference_date"] = "2026-10-22T09:30:00Z"
    pinned = execute_pinned(compile_pinned_plan(config), config["global_settings"]["seed"])["t"]
    assert pinned.schema.field("x").type == _spec_type(x["output_type"])
    assert (
        pinned.column("x").to_pylist() == generate(_legacy(helpers, x))["t"].column("x").to_pylist()
    )


def test_decimal_rounds_half_even_on_the_decimal_value_of_the_float() -> None:
    cases = [
        (12.345, "12.34"),
        (12.355, "12.36"),
        (0.125, "0.12"),
        (2.675, "2.68"),
        (-12.345, "-12.34"),
    ]
    for value, want in cases:
        col = _col("x", "constant", "decimal(2)", value=value)
        got = generate(one(col, rows=1))["t"].column("x").to_pylist()
        assert got == [D(want)], value


def test_a_null_stays_null_and_the_column_keeps_its_declared_type() -> None:
    col = _col("x", "constant", "decimal(2)", value=1.5, null_probability=1.0)
    for config in (one(col, rows=6), _legacy([], col)):
        out = generate(config)["t"]
        assert out.schema.field("x").type == pa.decimal128(38, 2)
        assert out.column("x").to_pylist()[:6] == [None] * 6


# AT-8: the cast has two call sites; removing either one must turn the typed output red. -------


def test_the_resident_cast_site_is_load_bearing(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.generation import _statistical_column as site

    col = _col("x", "constant", "decimal(2)", value=12.3)
    assert generate(_legacy([], col))["t"].schema.field("x").type == pa.decimal128(38, 2)
    monkeypatch.setattr(site, "cast_output_columns", lambda data, *a, **k: data)
    # Arrow refuses to build a decimal column from the raw float the generator made.
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError, TypeError)):
        generate(_legacy([], col))


def test_the_streamed_cast_site_is_load_bearing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from decoy_engine.generation import _streamed as site

    col = _col("x", "constant", "decimal(2)", value=12.3)
    monkeypatch.setattr(site, "cast_output_values", lambda values, *a, **k: values)
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError, TypeError)):
        run_streamed(one(col, rows=4), tmp_path)


def test_both_routes_call_the_one_cast_function() -> None:
    from decoy_engine.generation import _statistical_column, _streamed

    assert _streamed.cast_output_values is kinds.cast_output_values
    assert _statistical_column.cast_output_columns is kinds.cast_output_columns


# AT-9: a child reference reads the parent's emitted values. ----------------------------------


def _fk_config() -> dict[str, Any]:
    return cfg(
        [
            table("p", 8, [_col("id", "sequence", "int", start=100)]),
            table(
                "c",
                40,
                [
                    {
                        "name": "pid",
                        "type": "reference",
                        "reference_table": "p",
                        "reference_column": "id",
                    }
                ],
            ),
        ]
    )


def test_a_child_reference_into_a_declared_int_parent_carries_int64_resident() -> None:
    out = generate(_fk_config())
    assert out["p"].schema.field("id").type == pa.int64()
    assert out["c"].schema.field("pid").type == pa.int64()
    assert set(out["c"].column("pid").to_pylist()) <= set(out["p"].column("id").to_pylist())


def test_a_child_reference_into_a_declared_int_parent_carries_int64_streamed(
    tmp_path: Path,
) -> None:
    _, tables = run_streamed(_fk_config(), tmp_path)
    assert tables["c"].schema.field("pid").type == pa.int64()
    parents = set(tables["p"].column("id").to_pylist())
    assert set(tables["c"].column("pid").to_pylist()) <= parents


def test_a_child_reference_into_a_declared_decimal_parent_carries_the_decimal_type(
    tmp_path: Path,
) -> None:
    config = _fk_config()
    config["tables"][0]["generate_columns"][0]["output_type"] = "decimal(2)"
    out = generate(config)
    assert out["c"].schema.field("pid").type == pa.decimal128(38, 2)
    _, tables = run_streamed(config, tmp_path)
    assert tables["c"].schema.field("pid").type == pa.decimal128(38, 2)
    assert tables["c"].column("pid").to_pylist() == out["c"].column("pid").to_pylist()


# AT-15: a failed cast names the table, column and type, never a value. -----------------------

SENTINEL = "SENTINEL_CELL_4711"


def _fail(config: dict[str, Any]) -> Any:
    from decoy_engine.execution import ExecutionError

    with pytest.raises(ExecutionError) as info:
        generate(config)
    return info.value


@pytest.mark.parametrize(
    ("col", "kernel_code"),
    [
        (_col("x", "constant", "int", value=SENTINEL), "declared_integer_invalid"),
        (_col("x", "constant", "int", value=2.5), None),
        (_col("x", "constant", "int", value=True), None),
        (_col("x", "constant", "date", value=SENTINEL), "declared_date_invalid"),
        (_col("x", "constant", "bool", value=SENTINEL), None),
        (_col("x", "constant", "decimal(2)", value=SENTINEL), "declared_decimal_invalid"),
        (_col("x", "constant", "decimal(2)", value=float(10**40)), "declared_decimal_overflow"),
        (_col("x", "constant", "int", value=2**62), None),
    ],
    ids=[
        "int-text",
        "int-fraction",
        "int-bool",
        "date-text",
        "bool-text",
        "dec-text",
        "dec-overflow",
        "int-ok",
    ],
)
def test_a_cast_that_fails_names_table_column_type_and_no_value(
    col: dict[str, Any], kernel_code: str | None
) -> None:
    if col["value"] == 2**62:
        generate(one(col, rows=2))  # in range: no failure
        return
    for config in (one(col, rows=3), _legacy([], col)):
        err = _fail(config)
        assert err.code == "output_type_cast_failed"
        text = str(err)
        assert "'t'" in text and "'x'" in text and col["output_type"] in text
        if kernel_code:
            assert kernel_code in text
        assert SENTINEL not in text
        assert "2.5" not in text and str(10**40) not in text
        assert err.__cause__ is None and err.__context__ is None


def test_a_cast_failure_in_the_streamed_sink_route_has_the_same_text(tmp_path: Path) -> None:
    from decoy_engine.execution import ExecutionError

    col = _col("x", "constant", "int", value=SENTINEL)
    with pytest.raises(ExecutionError) as info:
        run_streamed(one(col, rows=3), tmp_path)
    assert info.value.code == "output_type_cast_failed"
    assert SENTINEL not in str(info.value)


def test_a_distribution_with_a_fraction_cannot_be_an_int() -> None:
    col = _col("x", "distribution", "int", family="uniform", low=0, high=1)
    assert _fail(one(col, rows=3)).code == "output_type_cast_failed"


# to_lexeme --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "output", "want"),
    [
        (5, "int", "5"),
        (5.0, "int", "5"),
        ("12", "int", "12"),
        (1.25, "decimal(1)", "1.2"),
        (D("3.456"), "decimal(2)", "3.46"),
        (3, "decimal(2)", "3.00"),
        ("2.5", "decimal(0)", "2"),
        (date(2026, 1, 2), "date", "2026-01-02"),
        ("2026-01-02", "date", "2026-01-02"),
        (True, "bool", "true"),
        (False, "bool", "false"),
        (1, "bool", "1"),
        (0, "bool", "0"),
        ("TRUE", "bool", "TRUE"),
    ],
)
def test_to_lexeme(value: Any, output: str, want: str) -> None:
    out = kinds.output_type_of({"output_type": output})
    assert out is not None
    assert kinds.to_lexeme(value, out).value == want


@pytest.mark.parametrize(
    ("value", "output"),
    [
        (True, "int"),
        (2.5, "int"),
        (float("nan"), "decimal(2)"),
        (float("inf"), "decimal(2)"),
        (None, "int"),
        ([1], "int"),
        (5, "date"),
        (True, "date"),
        (2, "bool"),
    ],
)
def test_to_lexeme_refuses_what_has_no_honest_text(value: Any, output: str) -> None:
    out = kinds.output_type_of({"output_type": output})
    assert out is not None
    assert not kinds.to_lexeme(value, out).ok


def test_no_output_type_means_no_cast() -> None:
    assert kinds.output_type_of({"name": "x", "type": "constant", "value": 1}) is None


# AT-17: the values before the cast equal the values without `output_type`. -------------------


@pytest.mark.parametrize(("label", "helpers", "x", "routes", "first", "text"), CELLS, ids=IDS)
def test_values_before_the_cast_do_not_depend_on_output_type(
    label: str, helpers: list[dict[str, Any]], x: dict[str, Any], routes: str, first: Any, text: str
) -> None:
    from decoy_engine.generation import _statistical_column as site

    seen: dict[str, list[Any]] = {}
    real = site.cast_output_columns

    def spy(data: dict[str, list[Any]], *a: Any, **k: Any) -> Any:
        seen.update({n: list(v) for n, v in data.items()})
        return real(data, *a, **k)

    bare = {k: v for k, v in x.items() if k != "output_type"}
    plain = generate(_legacy(helpers, bare))["t"].column("x").to_pylist()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(site, "cast_output_columns", spy)
        generate(_legacy(helpers, x))
    finally:
        mp.undo()
    # The generator's own values, taken at the cast boundary, equal the untyped column's output
    # (a numeric sequence or faker is already int there; text and bools pass unchanged).
    assert seen["x"] == plain
