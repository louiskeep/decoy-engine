"""Regression test for G6: formula columns with `references` returning all-None.

Before the fix, ColumnGenerator._generate_formula_column returned a
None-filled placeholder when a column had `references: [...]`, expecting
a post-pass that generate_op never ran.  The referenced-formula post-pass
(pass 3) in generate_op.apply now fills these columns using sibling values
already present in the output DataFrame.
"""
from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.graph.ops import generate_op


class _MockCtx:
    """Minimal context stand-in — no pool_resolver needed for this test."""

    _current_node_id = "gen_1"
    column_relationships: list = []
    pool_resolver = None
    pipeline_derive_key = None
    instance_default_locale = None

    def __init__(self):
        self._exports: dict = {}
        self._warnings: list = []

    def export(self, key: str, value) -> None:
        self._exports[key] = value

    @property
    def logger(self):
        return self

    def warning(self, msg: str) -> None:
        self._warnings.append(msg)

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass


# ── Core regression tests ────────────────────────────────────────────────────


def test_formula_with_references_produces_non_none():
    """G6 regression: formula column with references must not be all-None."""
    config = {
        "row_count": 5,
        "seed": 0,
        "columns": {
            "first_name": {"strategy": "faker", "faker_type": "first_name"},
            "last_name": {"strategy": "faker", "faker_type": "last_name"},
            "full_name": {
                "strategy": "formula",
                "formula": "f\"{first_name} {last_name}\"",
                "references": ["first_name", "last_name"],
            },
        },
    }
    ctx = _MockCtx()
    result = generate_op.apply([], config, ctx)

    assert isinstance(result, pd.DataFrame)
    assert "full_name" in result.columns
    # The core bug: every value was None before the fix.
    assert result["full_name"].notna().all(), (
        f"Expected no None in full_name, got: {result['full_name'].tolist()}"
    )


def test_formula_with_references_uses_sibling_values():
    """Sibling column values must appear in the evaluated formula output."""
    config = {
        "row_count": 3,
        "seed": 42,
        "columns": {
            "first": {"strategy": "faker", "faker_type": "first_name"},
            "last": {"strategy": "faker", "faker_type": "last_name"},
            "combined": {
                "strategy": "formula",
                "formula": "first + '|' + last",
                "references": ["first", "last"],
            },
        },
    }
    ctx = _MockCtx()
    result = generate_op.apply([], config, ctx)

    for i, row in result.iterrows():
        expected = f"{row['first']}|{row['last']}"
        assert row["combined"] == expected, (
            f"Row {i}: expected {expected!r}, got {row['combined']!r}"
        )


def test_formula_without_references_still_evaluates_inline():
    """Formula columns with no references must still produce values (pass 1)."""
    config = {
        "row_count": 4,
        "seed": 0,
        "columns": {
            "seq": {
                "strategy": "formula",
                "formula": "i * 10",
            },
        },
    }
    ctx = _MockCtx()
    result = generate_op.apply([], config, ctx)

    assert result["seq"].notna().all()
    assert list(result["seq"]) == [0, 10, 20, 30]


def test_formula_references_missing_column_emits_none_with_warning():
    """When a referenced column is absent, the formula column should be None
    and a warning should be logged rather than raising."""
    config = {
        "row_count": 3,
        "seed": 0,
        "columns": {
            "derived": {
                "strategy": "formula",
                "formula": "absent_col + '_x'",
                "references": ["absent_col"],
            },
        },
    }
    ctx = _MockCtx()
    result = generate_op.apply([], config, ctx)

    assert "derived" in result.columns
    # All None because the referenced column doesn't exist.
    assert result["derived"].isna().all()
    # A warning must have been emitted.
    assert any("absent_col" in w for w in ctx._warnings), (
        f"Expected a warning mentioning 'absent_col', got: {ctx._warnings}"
    )


def test_formula_references_deterministic_across_runs():
    """Same seed + same config must yield bit-identical outputs."""
    config = {
        "row_count": 5,
        "seed": 7,
        "columns": {
            "x": {"strategy": "sequence", "start": 1, "step": 1},
            "doubled": {
                "strategy": "formula",
                "formula": "int(x) * 2",
                "references": ["x"],
            },
        },
    }
    ctx1 = _MockCtx()
    ctx2 = _MockCtx()
    r1 = generate_op.apply([], config, ctx1)
    r2 = generate_op.apply([], config, ctx2)

    assert list(r1["doubled"]) == list(r2["doubled"])
