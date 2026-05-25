"""Unit tests for ApplyContext (V2 Phase 3 D5c).

The context object itself is intentionally tiny; these tests pin
the contract that future strategies + dispatcher work depends on:
  - frozen / immutable
  - empty default
  - read-only joint_columns mapping (mutation attempts raise)
  - .empty() returns a valid instance with no joint data
"""
from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.transforms.apply_context import ApplyContext


def test_empty_context_has_no_joint_columns() -> None:
    ctx = ApplyContext.empty()
    assert dict(ctx.joint_columns) == {}


def test_default_construction_is_empty() -> None:
    ctx = ApplyContext()
    assert dict(ctx.joint_columns) == {}


def test_context_is_frozen() -> None:
    """Strategies must not be able to swap the joint_columns field."""
    ctx = ApplyContext.empty()
    with pytest.raises((AttributeError, TypeError)):
        ctx.joint_columns = {"x": pd.Series([1, 2])}  # type: ignore[misc]


def test_constructed_with_joint_columns_passes_through() -> None:
    s = pd.Series(["a", "b", "c"])
    ctx = ApplyContext(joint_columns={"city": s})
    pd.testing.assert_series_equal(ctx.joint_columns["city"], s)


def test_empty_default_is_module_level_singleton_for_zero_cost() -> None:
    """Two empty contexts share the same empty mapping object so
    constructing an empty ApplyContext at every dispatcher call is
    near-free."""
    c1 = ApplyContext.empty()
    c2 = ApplyContext.empty()
    assert c1.joint_columns is c2.joint_columns
