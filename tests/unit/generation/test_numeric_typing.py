"""Generated numbers stay numbers through column assembly and derived arithmetic."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.config._fixed_width import FixedWidthLayout
from tests.parity._f0_fixtures import config, run_generate_tables, table


def _run(cols: list[dict], rows: int = 4) -> pa.Table:
    return run_generate_tables(config([table("t", rows, cols)]))["t"]


def _seq(name: str = "a", **kw: object) -> dict:
    return {"name": name, "type": "sequence", "start": 1, "step": 1, **kw}


def _derived(name: str, expression: str) -> dict:
    return {"name": name, "type": "derived", "expression": expression}


def test_int_times_int_is_numeric() -> None:
    t = _run([_seq(), _derived("b", "a * 2")])
    assert t.column("b").to_pylist() == [2, 4, 6, 8]


def test_int_times_float_is_numeric() -> None:
    t = _run([_seq(), _derived("b", "a * 0.7")])
    assert t.column("b").to_pylist() == pytest.approx([0.7, 1.4, 2.1, 2.8])


def test_float_times_int_is_numeric() -> None:
    cols = [
        {"name": "a", "type": "faker", "faker_type": "pyfloat", "faker_kwargs": {"min_value": 1}},
        _derived("b", "a * 2"),
    ]
    t = _run(cols)
    a = t.column("a").to_pylist()
    assert all(isinstance(v, float) for v in a)
    assert t.column("b").to_pylist() == pytest.approx([v * 2 for v in a])


def test_numeric_columns_have_numeric_arrow_types() -> None:
    t = _run([_seq(), {"name": "f", "type": "faker", "faker_type": "pyfloat"}])
    assert pa.types.is_integer(t.schema.field("a").type)
    assert pa.types.is_floating(t.schema.field("f").type)


def test_prefixed_sequence_stays_text() -> None:
    t = _run([_seq(prefix="ID-", pad_length=3)])
    assert t.column("a").to_pylist() == ["ID-001", "ID-002", "ID-003", "ID-004"]


def test_grouped_series_orders_numeric_order_by_numerically() -> None:
    cols = [
        {"name": "g", "type": "categorical", "categories": ["x"]},
        _seq("line_no", start=1),
        {
            "name": "n",
            "type": "grouped_series",
            "group_by": "g",
            "order_by": "line_no",
            "generator": "cumcount",
        },
    ]
    t = _run(cols, rows=12)
    assert t.column("n").to_pylist() == list(range(12))


def _numeric_table() -> pa.Table:
    return _run([_seq(), {"name": "f", "type": "faker", "faker_type": "pyfloat"}], rows=5)


def test_writers_emit_the_same_text_as_text_cells(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    from decoy_engine.profile._fixed_width_reader import read_fixed_width
    from decoy_engine.profile._fixed_width_writer import write_fixed_width

    t = _numeric_table()
    as_text = [
        (str(a), str(f)) for a, f in zip(t["a"].to_pylist(), t["f"].to_pylist(), strict=True)
    ]

    pandas_csv = t.to_pandas().to_csv(index=False).splitlines()[1:]
    assert pandas_csv == [f"{a},{f}" for a, f in as_text]

    pq.write_table(t, tmp_path / "t.parquet")
    back = pq.read_table(tmp_path / "t.parquet")
    assert back.schema.field("a").type == pa.int64()
    assert back["f"].to_pylist() == t["f"].to_pylist()

    layout = FixedWidthLayout(
        columns=[
            {"name": "a", "start": 0, "width": 4, "type": "int"},
            {"name": "f", "start": 4, "width": 24, "type": "float"},
        ]
    )
    out = write_fixed_width(t.to_pandas(), layout, str(tmp_path / "o.txt"))
    rows = Path(out).read_text().splitlines()
    assert [(r[:4].strip(), r[4:].strip()) for r in rows] == as_text
    assert read_fixed_width(out, layout)["f"].tolist() == t["f"].to_pylist()


def test_nullable_int_columns_write_csv_without_a_decimal_point() -> None:
    from decoy_engine.generation._numeric_typing import generated_table_to_pandas

    cols = [
        {**_seq(), "null_probability": 0.5},
        {
            "name": "p",
            "type": "faker",
            "faker_type": "pyint",
            "faker_kwargs": {"min_value": 10, "max_value": 99},
            "null_probability": 0.5,
        },
    ]
    t = _run(cols, rows=40)
    assert t["a"].null_count > 0 and t["p"].null_count > 0
    lines = generated_table_to_pandas(t).to_csv(index=False).splitlines()[1:]
    for line in lines:
        for cell in line.split(","):
            assert cell == "" or cell.isdigit(), cell
    assert "1.0" not in "".join(lines)


# ---- plan-time guards: a column is numeric only when it provably fits and is not a text key ----


def _both_routes(cols: list[dict], rows: int = 4) -> list[pa.Table]:
    """The streamed core and the legacy whole-table generator (forced by a fresh column)."""
    legacy_cols = [
        *cols,
        {"name": "z", "type": "categorical", "categories": ["a"], "determinism": "fresh"},
    ]
    return [_run(cols, rows), _run(legacy_cols, rows).select([c["name"] for c in cols])]


def test_sequence_that_overflows_int64_stays_text_on_both_routes() -> None:
    start, step = 2**63 - 2, 10**30
    want = [str(start + i * step) for i in range(4)]
    for t in _both_routes([_seq(start=start, step=step)]):
        assert t.schema.field("a").type == pa.string()
        assert t["a"].to_pylist() == want


def test_sequence_that_reaches_the_int64_edge_stays_numeric() -> None:
    for t in _both_routes([_seq(start=2**63 - 4)]):
        assert t.schema.field("a").type == pa.int64()
        assert t["a"].to_pylist() == [2**63 - 4 + i for i in range(4)]


def test_faker_random_number_too_wide_for_int64_stays_text_on_both_routes() -> None:
    col = {
        "name": "a",
        "type": "faker",
        "faker_type": "random_number",
        "faker_kwargs": {"digits": 20, "fix_len": True},
    }
    for t in _both_routes([col]):
        assert t.schema.field("a").type == pa.string()
        assert all(isinstance(v, str) and len(v) == 20 for v in t["a"].to_pylist())


@pytest.mark.parametrize(
    ("faker_type", "kwargs", "numeric"),
    [
        ("random_number", {"digits": 18}, True),
        ("random_number", {"digits": 19}, False),
        ("pyint", {"min_value": 0, "max_value": 2**63 - 1}, True),
        ("pyint", {"min_value": 0, "max_value": 2**63}, False),
        ("random_int", {"min": -(2**63), "max": 5}, True),
        ("unix_time", {}, False),
    ],
)
def test_faker_int_providers_are_numeric_only_when_bounded(
    faker_type: str, kwargs: dict, numeric: bool
) -> None:
    col = {"name": "a", "type": "faker", "faker_type": faker_type, "faker_kwargs": kwargs}
    for t in _both_routes([col]):
        assert pa.types.is_integer(t.schema.field("a").type) == numeric


def test_suffix_only_sequence_stays_text() -> None:
    t = _run([_seq(suffix="x")])
    assert t["a"].to_pylist() == ["1x", "2x", "3x", "4x"]


def test_custom_provider_under_a_stock_numeric_name_stays_text() -> None:
    from decoy_engine import register_faker_provider, unregister_faker_provider

    register_faker_provider("pyint", lambda f: "007")
    try:
        for t in _both_routes([{"name": "a", "type": "faker", "faker_type": "pyint"}]):
            assert t.schema.field("a").type == pa.string()
            assert t["a"].to_pylist() == ["007"] * 4
    finally:
        unregister_faker_provider("pyint")
