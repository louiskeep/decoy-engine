"""The option table in docs/strategies.md matches `generate_option_catalog()`.

The table is a hand-written copy of the catalog, so this test parses it and compares, and the doc
cannot drift from the registry that config parse, plan compile and `decoy schema` read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.config import generate_option_catalog
from decoy_engine.plan import PlanCompileError, compile_plan
from tests.unit._dps_helpers import empty_profile

_DOC = Path(__file__).resolve().parents[3] / "docs" / "strategies.md"
_HEADING = "### Options are checked per type"
_NAME = re.compile(r"`(\w+)`")
_WHEN = re.compile(r"((?:`\w+`(?:, )?)+) if `(\w+): (\w+)`")
_AT_PLAN = " (at plan compile)"

# One valid value for every key a type checks at plan compile, so each can be dropped in turn.
# Only test input: which keys are plan-checked comes from the catalog.
_VALID_AT_PLAN: dict[str, dict[str, Any]] = {"derived_aggregate": {"op": "sum", "column": "a"}}

Row = tuple[set[str], set[str], set[str], dict[str, dict[str, set[str]]]]


def _names(cell: str) -> set[str]:
    return set(_NAME.findall(cell))


def _when(cell: str) -> dict[str, dict[str, set[str]]]:
    if not cell:
        return {}
    match = _WHEN.fullmatch(cell)
    assert match, f"unreadable 'Required when' cell: {cell!r}"
    keys, selector, value = match.groups()
    return {selector: {value: _names(keys)}}


def _required(cell: str) -> tuple[set[str], set[str]]:
    """All required names, and the ones the cell marks as checked at plan compile."""
    if cell.endswith(_AT_PLAN):
        names = _names(cell.removesuffix(_AT_PLAN))
        return names, names
    return _names(cell), set()


def parse_table(text: str) -> dict[str, Row]:
    """Type -> (required, at plan compile, optional, required_when) from the table."""
    lines = text.split(_HEADING, 1)[1].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Type |"))
    table: dict[str, Row] = {}
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 4, f"row needs 4 cells: {line!r}"
        (type_,) = _names(cells[0])
        required, at_plan = _required(cells[1])
        table[type_] = (required, at_plan, _names(cells[2]), _when(cells[3]))
    return table


def catalog_table() -> dict[str, Row]:
    return {
        type_: (
            set(entry.required),
            set(entry.required_at_plan),
            set(entry.optional),
            {
                sel: {val: set(keys) for val, keys in by_value.items()}
                for sel, by_value in entry.required_when.items()
            },
        )
        for type_, entry in generate_option_catalog().items()
    }


def test_the_doc_table_matches_the_catalog() -> None:
    doc = parse_table(_DOC.read_text(encoding="utf-8"))
    assert set(doc) == set(generate_option_catalog())
    assert doc == catalog_table()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("| `faker_type` | `locale`, `faker_kwargs` |", "| `faker_type` | `locale` |"),
        ("| `op`, `column` (at plan compile) |", "| `op`, `column` |"),
    ],
    ids=["optional key dropped", "plan-compile marker dropped"],
)
def test_a_planted_mismatch_is_caught(old: str, new: str) -> None:
    text = _DOC.read_text(encoding="utf-8")
    planted = text.replace(old, new)
    assert planted != text
    assert parse_table(planted) != catalog_table()


def test_a_when_cell_with_trailing_text_is_unreadable() -> None:
    with pytest.raises(AssertionError, match="unreadable"):
        _when("`low`, `high` if `family: uniform` and more")


def _plan_checked() -> list[tuple[str, str]]:
    return [
        (type_, key)
        for type_, entry in generate_option_catalog().items()
        for key in entry.required_at_plan
    ]


def test_every_plan_checked_type_has_valid_test_values() -> None:
    types = {type_ for type_, _ in _plan_checked()}
    assert types == set(_VALID_AT_PLAN)
    for type_, key in _plan_checked():
        assert key in _VALID_AT_PLAN[type_]


@pytest.mark.parametrize(("type_", "key"), _plan_checked())
def test_a_plan_checked_key_is_refused_at_plan_compile(type_: str, key: str) -> None:
    options: Mapping[str, Any] = {k: v for k, v in _VALID_AT_PLAN[type_].items() if k != key}
    config = {
        "global_settings": {"seed": 1},
        "tables": [
            {
                "name": "t",
                "row_count": 3,
                "generate_columns": [
                    {"name": "a", "type": "sequence", "start": 1},
                    {"name": "x", "type": type_, **options},
                ],
            }
        ],
    }
    with pytest.raises(PlanCompileError) as exc:
        compile_plan(
            config, empty_profile(), decoy_engine_version="test", instance_default_locale=None
        )
    assert str(exc.value.code) == f"{type_}_{key}_missing"
