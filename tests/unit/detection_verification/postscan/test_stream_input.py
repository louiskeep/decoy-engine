"""One table read three ways (resident, Parquet, CSV) gives the same batches."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config._csv_dialect import CsvDialect
from decoy_engine.execution._chunked_input import check_opened, facts_from_footer
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.profile._lazy_csv import CsvLazySource
from decoy_engine.profile._readers import LazySource
from decoy_engine.verification.postmask import (
    TableBatches,
    from_lazy,
    from_table,
    key_batches,
    missing_columns,
)

# String columns only: CSV is read as text, so only text compares equal across formats.
ROWS = {
    "a": ["1", "2", "3", "4", "5", "6", "7"],
    "b": ["x", "y", "z", "x", "y", "z", "x"],
    "c": ["p", "q", "r", "s", "t", "u", "v"],
}
BATCH = 3


def _all_three(tmp_path: Path) -> dict[str, TableBatches]:
    table = pa.table(ROWS)
    pq_path = tmp_path / "t.parquet"
    pq.write_table(table, pq_path, row_group_size=4)
    csv_path = tmp_path / "t.csv"
    csv_path.write_text(
        "a,b,c\n" + "".join(f"{a},{b},{c}\n" for a, b, c in zip(*ROWS.values(), strict=True))
    )
    return {
        "table": from_table("t", table),
        "parquet": from_lazy("t", LazySource(pq_path)),
        "csv": from_lazy("t", CsvLazySource(csv_path, dialect=CsvDialect(), table="t")),
    }


def _read(table: TableBatches, columns: list[str]) -> tuple[pa.Table, list[int]]:
    batches = list(table.read(columns, BATCH))
    return pa.Table.from_batches(batches), [b.num_rows for b in batches]


@pytest.mark.parametrize("columns", [["a", "b", "c"], ["c", "a"], ["b"]])
def test_three_sources_read_identically(tmp_path: Path, columns: list[str]) -> None:
    inputs = _all_three(tmp_path)
    expected = pa.table({c: ROWS[c] for c in columns})
    for name, table in inputs.items():
        got, sizes = _read(table, columns)
        assert got.to_pydict() == expected.to_pydict(), name
        assert table.num_rows == 7, name
        assert sum(sizes) == 7 and max(sizes) <= BATCH, name


def test_missing_columns_and_key_labels(tmp_path: Path) -> None:
    for table in _all_three(tmp_path).values():
        assert missing_columns(table, ["a", "zz", "c", "yy"]) == ("zz", "yy")
        assert missing_columns(table, ["a"]) == ()
        keys = key_batches(table, ["c", "a"])
        assert keys.schema.names == ["k0", "k1"]
        rows = pa.Table.from_batches(list(keys.batches()), schema=keys.schema)
        assert rows.to_pydict() == {"k0": ROWS["c"], "k1": ROWS["a"]}
        assert len(list(keys.batches())) == len(list(keys.batches()))  # re-callable


def test_resident_is_marked_and_lazy_is_not(tmp_path: Path) -> None:
    inputs = _all_three(tmp_path)
    assert inputs["table"].resident is not None
    assert inputs["parquet"].resident is None and inputs["csv"].resident is None


@pytest.mark.parametrize("kind", ["parquet", "csv"])
def test_projected_open_passes_check_opened(tmp_path: Path, kind: str) -> None:
    """The projection narrows the batches only; the facts stay the whole file's."""
    pq_path, csv_path = tmp_path / "t.parquet", tmp_path / "t.csv"
    pq.write_table(pa.table(ROWS), pq_path, row_group_size=4)
    csv_path.write_text("a,b,c\n" + "".join(f"{i},x,y\n" for i in range(7)))
    source: LazySource = (
        LazySource(pq_path)
        if kind == "parquet"
        else CsvLazySource(csv_path, dialect=CsvDialect(), table="t")
    )
    full = source.open_batches(BATCH)
    expected = facts_from_footer(full.facts)
    full.close()

    opened = source.open_batches(BATCH, columns=["b"])
    try:
        check_opened(opened, expected, table="t")
        assert opened.schema.names == ["a", "b", "c"]
        batches = list(opened.batches)
    finally:
        opened.close()
    assert batches and all(b.schema.names == ["b"] for b in batches)

    seen: list[object] = []
    table = from_lazy(
        "t", source, on_open=lambda o: (seen.append(o), check_opened(o, expected, table="t"))
    )
    assert sum(b.num_rows for b in table.read(["a"], BATCH)) == 7
    assert len(seen) == 1


def test_changed_file_fails_the_on_open_check(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table(ROWS), path)
    source = LazySource(path)
    opened = source.open_batches(BATCH)
    expected = facts_from_footer(opened.facts)
    opened.close()
    pq.write_table(pa.table({k: v[:-1] for k, v in ROWS.items()}), path)
    table = from_lazy("t", source, on_open=lambda o: check_opened(o, expected, table="t"))
    with pytest.raises(ExecutionError):
        list(table.read(["a"], BATCH))
