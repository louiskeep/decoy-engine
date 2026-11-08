"""A generated column is numeric only when every reader of it is numeric-safe.

Each case runs on the generate core, and through `run_pipeline` in `full_frame` and `auto`; the
output must equal what the text-typed generator gave before numeric typing.
"""

from __future__ import annotations

import copy
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.generation._numeric_typing import numeric_plan
from tests.parity import _f2_mixed_support as s
from tests.parity._f0_fixtures import config, run_generate_tables, table

MODES = ["full_frame", "auto"]


def _cfg(cols: list[dict[str, Any]], rows: int = 6) -> dict[str, Any]:
    raw = config([table("t", rows, cols)])
    raw["targets"] = {"t": {"type": "file", "format": "csv", "path": "t.csv"}}
    return raw


def _outputs(cfg: dict[str, Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> list[pa.Table]:
    monkeypatch.chdir(tmp_path)
    out = [run_generate_tables(cfg)["t"]]
    out += [s.run(copy.deepcopy(cfg), {}, mode=m).outputs["t"] for m in MODES]
    return out


SEQ = {"name": "id", "type": "sequence", "start": 1, "step": 1}


@pytest.mark.parametrize(
    ("formula", "want"),
    [
        ("'ORD-' + id", [f"ORD-{i}" for i in range(1, 7)]),
        ("id.zfill(5)", [f"{i:05d}" for i in range(1, 7)]),
        ("'ORD-' + str(id)", [f"ORD-{i}" for i in range(1, 7)]),
    ],
)
def test_referenced_formula_reads_the_text_the_sequence_always_gave(
    formula: str, want: list[str], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cols = [SEQ, {"name": "f", "type": "formula", "formula": formula, "references": ["id"]}]
    for t in _outputs(_cfg(cols), tmp_path, monkeypatch):
        assert t["f"].to_pylist() == want


def test_referenced_formula_over_a_nullable_sequence_has_no_float_text(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cols = [
        {**SEQ, "null_probability": 0.5},
        {"name": "f", "type": "formula", "formula": "'ORD-' + str(id)", "references": ["id"]},
    ]
    for t in _outputs(_cfg(cols, rows=40), tmp_path, monkeypatch):
        ids = t["id"].to_pylist()
        for i, (idv, f) in enumerate(zip(ids, t["f"].to_pylist(), strict=True)):
            assert f is None or ".0" not in f
            if idv is not None:
                assert f == f"ORD-{i + 1}"


def _two_tables(
    child_cols: list[dict[str, Any]], parent_cols: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    raw = config(
        [
            table("people", 6, parent_cols or [SEQ]),
            table("accounts", 6, child_cols),
        ]
    )
    raw["relationships"] = [
        {
            "parent": {"table": "people", "columns": ["id"]},
            "children": [{"table": "accounts", "columns": ["pid"]}],
            "orphan_policy": "preserve",
            "namespace": "ns",
        }
    ]
    return raw


def test_relationship_with_a_categorical_text_child_keeps_both_sides_text() -> None:
    raw = _two_tables([{"name": "pid", "type": "categorical", "categories": ["0", "1", "2"]}])
    out = run_generate_tables(raw)
    assert out["people"].schema.field("id").type == pa.string()
    assert out["accounts"].schema.field("pid").type == pa.string()


def test_relationship_with_a_matching_reference_child_stays_numeric() -> None:
    child = [
        {"name": "pid", "type": "reference", "reference_table": "people", "reference_column": "id"}
    ]
    out = run_generate_tables(_two_tables(child))
    assert out["people"].schema.field("id").type == pa.int64()
    assert out["accounts"].schema.field("pid").type == pa.int64()


def test_relationship_parent_that_is_a_reference_column_keeps_the_root_text() -> None:
    raw = config(
        [
            table("roots", 6, [SEQ]),
            table(
                "people",
                6,
                [
                    {
                        "name": "id",
                        "type": "reference",
                        "reference_table": "roots",
                        "reference_column": "id",
                    }
                ],
            ),
            table(
                "accounts", 6, [{"name": "pid", "type": "categorical", "categories": ["1", "2"]}]
            ),
        ]
    )
    raw["relationships"] = [
        {
            "parent": {"table": "people", "columns": ["id"]},
            "children": [{"table": "accounts", "columns": ["pid"]}],
            "orphan_policy": "preserve",
            "namespace": "ns",
        }
    ]
    out = run_generate_tables(raw)
    assert out["roots"].schema.field("id").type == pa.string()
    assert out["people"].schema.field("id").type == pa.string()


def test_one_generated_child_and_one_masked_child_keep_the_parent_text() -> None:
    raw = _two_tables(
        [
            {
                "name": "pid",
                "type": "reference",
                "reference_table": "people",
                "reference_column": "id",
            }
        ]
    )
    raw["tables"].append(
        {"name": "masked", "columns": [{"name": "pid", "strategy": "passthrough"}]}
    )
    raw["relationships"][0]["children"].append({"table": "masked", "columns": ["pid"]})
    assert ("people", "id") not in numeric_plan(raw)


@pytest.mark.parametrize(
    ("reader", "numeric"),
    [
        ({"type": "statistical", "condition_on": "id"}, False),
        ({"type": "formula", "formula": "id", "references": ["id"]}, False),
        ({"type": "derived_aggregate", "op": "sum", "column": "id"}, False),
        ({"type": "grouped_series", "group_by": "id", "order_by": "other"}, False),
        ({"type": "group_key", "group_by": "id"}, False),
        ({"type": "windowed_date", "anchor": "id", "min_days": 1, "max_days": 3}, False),
        ({"type": "derived", "expression": "id * 2"}, True),
        ({"type": "grouped_series", "group_by": "other", "order_by": "id"}, True),
    ],
    ids=[
        "condition_on",
        "formula",
        "derived_aggregate",
        "group_by",
        "group_key",
        "anchor",
        "derived",
        "order_by",
    ],
)
def test_each_reader_kind_decides_whether_the_sequence_is_numeric(
    reader: dict[str, Any], numeric: bool
) -> None:
    other = {"name": "other", "type": "categorical", "categories": ["a", "b"]}
    cols = [SEQ, other, {"name": "r", **reader}]
    assert (("t", "id") in numeric_plan(config([table("t", 6, cols)]))) is numeric
