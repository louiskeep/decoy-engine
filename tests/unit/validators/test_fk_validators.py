"""TDD tests for FK validator error-raising on misconfigured parent tables/columns.

Written BEFORE the production fixes per the CLAUDE.md "land the assertion test
first" rule.

Covers:
  L1 - missing parent table in outputs raises a clear ValueError (not silent
       mass-flagging of every child FK).
  L1 - unknown parent column in parent table raises a clear ValueError.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.validators._fk_validators import validate_fk_intact

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_with_rel(
    parent_table: str,
    parent_cols: list[str],
    child_table: str,
    child_cols: list[str],
) -> dict[str, Any]:
    return {
        "relationships": [
            {
                "parent": {"table": parent_table, "columns": parent_cols},
                "children": [{"table": child_table, "columns": child_cols}],
                "orphan_policy": "fail",
            }
        ]
    }


# ---------------------------------------------------------------------------
# L1 - missing parent table
# ---------------------------------------------------------------------------


class TestL1MissingParentTable:
    """validate_fk_intact raises when the parent table is absent from outputs."""

    def test_missing_parent_raises(self) -> None:
        child = pa.table({"child_id": [1, 2, 3], "parent_fk": [10, 20, 99]})
        # "parents" table intentionally NOT in outputs
        outputs = {"children": child}
        config = _config_with_rel("parents", ["id"], "children", ["parent_fk"])
        with pytest.raises(ValueError) as exc_info:
            validate_fk_intact(outputs, {}, config)
        msg = str(exc_info.value).lower()
        assert "parent" in msg or "parents" in msg

    def test_present_parent_no_raise(self) -> None:
        """When the parent IS in outputs and FKs resolve, no raise."""
        parent = pa.table({"id": [10, 20]})
        child = pa.table({"child_id": [1, 2], "parent_fk": [10, 20]})
        outputs = {"parents": parent, "children": child}
        config = _config_with_rel("parents", ["id"], "children", ["parent_fk"])
        findings = validate_fk_intact(outputs, {}, config)
        assert findings == ()


# ---------------------------------------------------------------------------
# L1 - unknown parent column
# ---------------------------------------------------------------------------


class TestL1UnknownParentColumn:
    """validate_fk_intact raises when a parent PK column does not exist in the parent table."""

    def test_unknown_column_raises(self) -> None:
        parent = pa.table({"id": [10, 20]})
        child = pa.table({"child_id": [1, 2], "parent_fk": [10, 20]})
        outputs = {"parents": parent, "children": child}
        # "bad_pk_col" does not exist in "parents"
        config = _config_with_rel("parents", ["bad_pk_col"], "children", ["parent_fk"])
        with pytest.raises(ValueError) as exc_info:
            validate_fk_intact(outputs, {}, config)
        msg = str(exc_info.value).lower()
        assert "bad_pk_col" in msg or "column" in msg

    def test_known_column_no_raise(self) -> None:
        parent = pa.table({"id": [10, 20]})
        child = pa.table({"child_id": [1, 2], "parent_fk": [10, 20]})
        outputs = {"parents": parent, "children": child}
        config = _config_with_rel("parents", ["id"], "children", ["parent_fk"])
        findings = validate_fk_intact(outputs, {}, config)
        assert findings == ()
