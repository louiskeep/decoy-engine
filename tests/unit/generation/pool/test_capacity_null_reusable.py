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
    module_doc = __import__("decoy_engine._capacity", fromlist=["_"]).__doc__
    assert "must hold distinct values" in module_doc


def test_the_pool_export_is_a_facade_over_the_foundation_leaf(monkeypatch):
    """GEN-REL: config and generation share one predicate without a config-to-generation import."""
    import decoy_engine._capacity as foundation

    from decoy_engine.generation.pool import _capacity as facade

    assert foundation.unique_capacity_ok(5, 6) is False
    seen = []

    def spy(pool_size, rows, null_reusable=False):
        seen.append((pool_size, rows, null_reusable))
        return True

    monkeypatch.setattr(foundation, "unique_capacity_ok", spy)
    assert unique_capacity_ok(5, 6) is True
    assert facade.unique_capacity_ok(0, 9, null_reusable=True) is True
    assert seen == [(5, 6, False), (0, 9, True)]


def test_the_foundation_leaf_imports_nothing_from_the_engine():
    import ast
    from pathlib import Path

    import decoy_engine._capacity as foundation

    tree = ast.parse(Path(foundation.__file__).read_text(encoding="utf-8"))
    imported = [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
    ] + [a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names]
    assert imported == []
