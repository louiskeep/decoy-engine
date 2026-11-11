"""The option table in docs/strategies.md matches GENERATE_OPTIONS and CONDITIONAL_REQUIRED.

The table is a hand-written copy of the registry, so this test parses it and compares, and the
doc cannot drift from what config parse and plan compile accept.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from decoy_engine import reasons
from decoy_engine.config._generate_options import CONDITIONAL_REQUIRED, GENERATE_OPTIONS
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

_DOC = Path(__file__).resolve().parents[3] / "docs" / "strategies.md"
_HEADING = "### Options are checked per type"
_NAME = re.compile(r"`(\w+)`")
_WHEN = re.compile(r"^((?:`\w+`(?:, )?)+) if `(\w+): (\w+)`$")

# Keys the table lists as required that config parse leaves optional: the type's transform
# refuses them at plan compile instead. Each is pinned to its refusal below.
_PLAN_REQUIRED = {"derived_aggregate": {"op", "column"}}

Table = dict[str, tuple[set[str], set[str], dict[str, dict[str, set[str]]]]]


def _names(cell: str) -> set[str]:
    return set(_NAME.findall(cell))


def _when(cell: str) -> dict[str, dict[str, set[str]]]:
    if not cell:
        return {}
    match = _WHEN.match(cell)
    assert match, f"unreadable 'Required when' cell: {cell!r}"
    keys, selector, value = match.groups()
    return {selector: {value: _names(keys)}}


def parse_table(text: str) -> Table:
    """Type -> (required, optional, required_when) from the first table under the heading."""
    lines = text.split(_HEADING, 1)[1].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Type |"))
    table: Table = {}
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 4, f"row needs 4 cells: {line!r}"
        (type_,) = _names(cells[0])
        table[type_] = (_names(cells[1]), _names(cells[2]), _when(cells[3]))
    return table


def registry_table() -> Table:
    out: Table = {}
    for type_, spec in GENERATE_OPTIONS.items():
        required = set(spec.required) | _PLAN_REQUIRED.get(type_, set())
        when = {
            sel: {val: set(keys) for val, keys in by_value.items()}
            for sel, by_value in CONDITIONAL_REQUIRED.get(type_, {}).items()
        }
        out[type_] = (required, set(spec.keys) - required, when)
    return out


def test_the_doc_table_matches_the_registry() -> None:
    doc = parse_table(_DOC.read_text(encoding="utf-8"))
    assert set(doc) == set(GENERATE_OPTIONS)
    assert doc == registry_table()


def test_a_planted_mismatch_is_caught() -> None:
    text = _DOC.read_text(encoding="utf-8")
    planted = text.replace(
        "| `faker_type` | `locale`, `faker_kwargs` |", "| `faker_type` | `locale` |"
    )
    assert planted != text
    assert parse_table(planted) != registry_table()


@pytest.mark.parametrize(
    ("cfg", "code"),
    [
        ({"op": "", "column": "amount"}, reasons.plan.DERIVED_AGGREGATE_OP_MISSING),
        ({"op": "sum", "column": ""}, reasons.plan.DERIVED_AGGREGATE_COLUMN_MISSING),
    ],
)
def test_plan_required_keys_are_refused_at_plan_compile(cfg: dict[str, str], code: object) -> None:
    with pytest.raises(PlanCompileError) as exc:
        DerivedAggregateConfig.from_dict(cfg)
    assert str(exc.value.code) == str(code)
