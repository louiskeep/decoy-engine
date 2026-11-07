"""Lattice: shape, neighbours by brute force, node refusals, apply."""

from __future__ import annotations

import copy
import itertools

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.anonymize.hierarchies import (
    Lattice,
    date_hierarchy,
    interval_hierarchy,
    prefix_hierarchy,
)

AGE = interval_hierarchy((5, 10, 20))
ZIP = prefix_hierarchy(5)
SEEN = date_hierarchy()


def _lattice() -> Lattice:
    return Lattice({"age": AGE, "zip": ZIP, "seen": SEEN})


def test_shape() -> None:
    lattice = _lattice()
    assert lattice.columns == ("age", "zip", "seen")
    assert lattice.heights == (4, 5, 4)
    assert lattice.size == 5 * 6 * 5
    assert lattice.bottom == (0, 0, 0) and lattice.top == (4, 5, 4)
    assert dict(lattice.hierarchies) == {"age": AGE, "zip": ZIP, "seen": SEEN}
    with pytest.raises(TypeError):
        lattice.hierarchies["age"] = ZIP  # type: ignore[index]


def test_nodes_and_neighbours_match_brute_force() -> None:
    lattice = Lattice({"a": interval_hierarchy((5, 10)), "b": prefix_hierarchy(3)})
    nodes = list(lattice.nodes())
    assert lattice.heights == (3, 3)
    assert nodes == list(itertools.product(range(4), range(4)))
    for node in nodes:
        above = tuple(
            n for n in nodes
            if all(x >= y for x, y in zip(n, node, strict=False)) and sum(n) - sum(node) == 1
        )  # fmt: skip
        below = tuple(
            n for n in nodes
            if all(x <= y for x, y in zip(n, node, strict=False)) and sum(node) - sum(n) == 1
        )  # fmt: skip
        assert sorted(lattice.successors(node)) == sorted(above)
        assert sorted(lattice.predecessors(node)) == sorted(below)
    assert lattice.successors((3, 3)) == ()
    assert lattice.predecessors((0, 0)) == ()


@pytest.mark.parametrize(
    "node", [(0, 0), (0, 0, 0, 0), (True, 0, 0), (-1, 0, 0), (5, 0, 0), [0, 0, 0], (0.0, 0, 0)]
)
def test_node_refusals(node: object) -> None:
    lattice = _lattice()
    assert not lattice.contains(node)
    with pytest.raises((TypeError, ValueError)):
        lattice.successors(node)  # type: ignore[arg-type]


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": [1, 2, 3],
            "age": [17, 34, None],
            "zip": ["02134", "90210", "02139"],
            "seen": pd.to_datetime(["2024-03-05", "2024-11-30", None]),
        }
    )


def test_apply_dataframe_leaves_input_alone() -> None:
    frame = _frame()
    before = copy.deepcopy(frame)
    out = _lattice().apply(frame, (1, 3, 2))
    pd.testing.assert_frame_equal(frame, before)
    assert list(out.columns) == ["id", "age", "zip", "seen"]
    assert out["id"].tolist() == [1, 2, 3]
    assert out["age"].tolist() == ["[15, 20)", "[30, 35)", None]
    assert out["zip"].tolist() == ["02***", "90***", "02***"]
    assert out["seen"].tolist() == ["2024Q1", "2024Q4", None]


def test_apply_arrow_table() -> None:
    table = pa.Table.from_pandas(_frame(), preserve_index=False)
    before = table
    out = _lattice().apply(table, (4, 0, 3))
    assert table.equals(before) and table.column("age").to_pylist() == [17.0, 34.0, None]
    assert out.column_names == ["id", "age", "zip", "seen"]
    assert out.column("age").to_pylist() == ["*", "*", "*"]
    assert out.column("zip").to_pylist() == ["02134", "90210", "02139"]
    assert out.column("seen").to_pylist() == ["2024", "2024", None]


def test_apply_refusals_name_column_only() -> None:
    lattice = _lattice()
    with pytest.raises(ValueError, match="'zip'"):
        lattice.apply(_frame().drop(columns=["zip"]), (0, 0, 0))
    dup = pd.concat([_frame(), _frame()[["zip"]]], axis=1)
    with pytest.raises(ValueError, match="'zip'"):
        lattice.apply(dup, (0, 0, 0))
    bad = _frame().assign(zip=["SECRET", "90210", "02139"])
    with pytest.raises(ValueError, match="'zip'") as exc:
        lattice.apply(bad, (0, 1, 0))
    assert "SECRET" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    with pytest.raises(TypeError):
        lattice.apply({"age": [1]}, (0, 0, 0))  # type: ignore[arg-type, unused-ignore]


@pytest.mark.parametrize("mapping", [{}, {"a": object()}, {1: AGE}, [("a", AGE)]])
def test_constructor_refusals(mapping: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        Lattice(mapping)  # type: ignore[arg-type]


def test_apply_arrow_keeps_level_zero_columns_and_field_metadata() -> None:
    schema = pa.schema(
        [
            pa.field("age", pa.int64(), nullable=False, metadata={b"unit": b"years"}),
            pa.field("zip", pa.string(), metadata={b"source": b"crm"}),
        ]
    )
    table = pa.Table.from_batches(
        [
            pa.record_batch([pa.array([17, 34]), pa.array(["02134", "90210"])], schema=schema),
            pa.record_batch([pa.array([51]), pa.array(["02139"])], schema=schema),
        ]
    )
    lattice = Lattice({"age": AGE, "zip": ZIP})
    out = lattice.apply(table, (2, 0))
    assert out.column("zip").num_chunks == 2 and out.column("zip").equals(table.column("zip"))
    assert out.schema.field("zip") == schema.field("zip")
    assert out.schema.field("zip").metadata == {b"source": b"crm"}
    age = out.schema.field("age")
    assert (age.type, age.nullable, age.metadata) == (pa.string(), False, {b"unit": b"years"})
    assert out.column("age").to_pylist() == ["[10, 20)", "[30, 40)", "[50, 60)"]
    pandas_out = lattice.apply(table.to_pandas(), (0, 1))
    assert pandas_out["age"].tolist() == [17, 34, 51]
