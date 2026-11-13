"""Chained column-level `derived` entries count as created columns.

The executor orders a `derived` node after the nodes writing the columns it reads, so a chain
of column-level derives runs. The created-column contract must accept the same chains, and
still refuse a chain whose root reads a column nobody supplies, and a cycle.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.execution._column_access import created_columns
from decoy_engine.providers_v2 import get_default_registry

REG = get_default_registry()
SOURCE = ("a", "b")


def _derived(name: str, expression: str) -> dict[str, Any]:
    return {"name": name, "strategy": "derived", "provider_config": {"expression": expression}}


def _created(columns: list[dict[str, Any]], transforms: list[dict[str, Any]] | None = None):
    table: dict[str, Any] = {"name": "t", "columns": columns}
    if transforms:
        table["transforms"] = transforms
    return created_columns(table, SOURCE, REG)


def test_two_step_column_derive_chain_is_created() -> None:
    assert _created([_derived("x", "a + 1"), _derived("y", "x * 2")]) == {"x", "y"}


def test_three_step_chain_is_created_whatever_the_declaration_order() -> None:
    columns = [_derived("z", "y + 1"), _derived("y", "x + 1"), _derived("x", "a + 1")]
    assert _created(columns) == {"x", "y", "z"}


def test_chain_rooted_at_a_missing_source_column_is_not_created() -> None:
    columns = [_derived("x", "nope + 1"), _derived("y", "x * 2"), _derived("z", "y + 1")]
    assert _created(columns) == frozenset()


def test_mixed_transform_derive_and_column_derive_chain_is_created() -> None:
    transforms = [{"op": "derive", "column": "t1", "expression": "a + 1"}]
    columns = [_derived("x", "t1 + 1"), _derived("y", "x + b")]
    assert _created(columns, transforms) == {"t1", "x", "y"}


def test_derive_cycle_is_not_created() -> None:
    columns = [_derived("x", "y + 1"), _derived("y", "x + 1"), _derived("w", "a + 1")]
    assert _created(columns) == {"w"}


def test_self_reading_derive_is_not_created() -> None:
    assert _created([_derived("x", "x + 1")]) == frozenset()
