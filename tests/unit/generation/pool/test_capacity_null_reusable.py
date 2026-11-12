"""GEN-UNIQUE E1: `unique_capacity_ok` stays the one size rule and gains a reusable-null flag."""

from __future__ import annotations

import pytest

from decoy_engine.generation.pool import unique_capacity_ok


@pytest.mark.parametrize(
    ("pool", "rows", "expected"),
    [(5, 5, True), (5, 6, False), (0, 0, True), (0, 1, False), (10, 3, True)],
)
def test_default_rule_is_unchanged(pool, rows, expected):
    assert unique_capacity_ok(pool, rows) is expected
    assert unique_capacity_ok(pool, rows, null_reusable=False) is expected


@pytest.mark.parametrize("pool", [0, 1, 5])
def test_reusable_null_makes_capacity_unbounded(pool):
    assert unique_capacity_ok(pool, 10**9, null_reusable=True) is True


def test_docstring_names_rows_that_must_hold_distinct_values_and_the_null_case():
    doc = unique_capacity_ok.__doc__ or ""
    assert "distinct" in doc
    assert "null_reusable" in doc
    module_doc = __import__("decoy_engine.generation.pool._capacity", fromlist=["_"]).__doc__
    assert "must hold distinct values" in module_doc
