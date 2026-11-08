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
