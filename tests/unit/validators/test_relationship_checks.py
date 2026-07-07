"""S4 (Sprint 2 honesty pack): parent_window_respected + reconciliation_holds.

TDD: written before the implementation. Both validators mirror
`_fk_validators.py`'s parent-first edge-lookup pattern (SDV HMA1); no second
join mechanism is invented (trap T8).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.validators import validate


def _rel(
    parent_table: str, parent_cols: list[str], child_table: str, child_cols: list[str]
) -> dict[str, Any]:
    return {
        "parent": {"table": parent_table, "columns": parent_cols},
        "children": [{"table": child_table, "columns": child_cols}],
        "orphan_policy": "fail",
    }


class TestParentWindowRespected:
    def test_all_in_window_passes(self) -> None:
        outputs = {
            "parents": pa.table(
                {"id": ["1"], "window_start": ["2026-01-01"], "window_end": ["2026-12-31"]}
            ),
            "children": pa.table(
                {"parent_id": ["1", "1"], "event_date": ["2026-03-01", "2026-06-15"]}
            ),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "parent_window_respected",
                    "params": {
                        "child_table": "children",
                        "child_column": "event_date",
                        "parent_table": "parents",
                        "window_start_column": "window_start",
                        "window_end_column": "window_end",
                    },
                }
            ],
            "relationships": [_rel("parents", ["id"], "children", ["parent_id"])],
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_out_of_window_child_date_fails(self) -> None:
        outputs = {
            "parents": pa.table(
                {"id": ["1"], "window_start": ["2026-01-01"], "window_end": ["2026-12-31"]}
            ),
            "children": pa.table({"parent_id": ["1"], "event_date": ["2027-01-01"]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "parent_window_respected",
                    "params": {
                        "child_table": "children",
                        "child_column": "event_date",
                        "parent_table": "parents",
                        "window_start_column": "window_start",
                        "window_end_column": "window_end",
                    },
                }
            ],
            "relationships": [_rel("parents", ["id"], "children", ["parent_id"])],
        }
        report = validate(outputs, config)
        assert report.passed is False
        assert report.findings[0].failing_row_indices == (0,)

    def test_unparseable_child_date_fails(self) -> None:
        outputs = {
            "parents": pa.table(
                {"id": ["1"], "window_start": ["2026-01-01"], "window_end": ["2026-12-31"]}
            ),
            "children": pa.table({"parent_id": ["1"], "event_date": ["not-a-date"]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "parent_window_respected",
                    "params": {
                        "child_table": "children",
                        "child_column": "event_date",
                        "parent_table": "parents",
                        "window_start_column": "window_start",
                        "window_end_column": "window_end",
                    },
                }
            ],
            "relationships": [_rel("parents", ["id"], "children", ["parent_id"])],
        }
        report = validate(outputs, config)
        assert report.passed is False

    def test_null_child_date_skipped(self) -> None:
        outputs = {
            "parents": pa.table(
                {"id": ["1"], "window_start": ["2026-01-01"], "window_end": ["2026-12-31"]}
            ),
            "children": pa.table({"parent_id": ["1"], "event_date": [None]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "parent_window_respected",
                    "params": {
                        "child_table": "children",
                        "child_column": "event_date",
                        "parent_table": "parents",
                        "window_start_column": "window_start",
                        "window_end_column": "window_end",
                    },
                }
            ],
            "relationships": [_rel("parents", ["id"], "children", ["parent_id"])],
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_unparseable_parent_bound_fails_every_mapped_row(self) -> None:
        outputs = {
            "parents": pa.table(
                {"id": ["1"], "window_start": ["garbage"], "window_end": ["2026-12-31"]}
            ),
            "children": pa.table({"parent_id": ["1"], "event_date": ["2026-03-01"]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "parent_window_respected",
                    "params": {
                        "child_table": "children",
                        "child_column": "event_date",
                        "parent_table": "parents",
                        "window_start_column": "window_start",
                        "window_end_column": "window_end",
                    },
                }
            ],
            "relationships": [_rel("parents", ["id"], "children", ["parent_id"])],
        }
        report = validate(outputs, config)
        assert report.passed is False

    def test_missing_relationship_edge_raises(self) -> None:
        outputs = {
            "parents": pa.table(
                {"id": ["1"], "window_start": ["2026-01-01"], "window_end": ["2026-12-31"]}
            ),
            "children": pa.table({"parent_id": ["1"], "event_date": ["2026-03-01"]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "parent_window_respected",
                    "params": {
                        "child_table": "children",
                        "child_column": "event_date",
                        "parent_table": "parents",
                        "window_start_column": "window_start",
                        "window_end_column": "window_end",
                    },
                }
            ],
            "relationships": [],
        }
        with pytest.raises(ValueError, match="parent_window_respected"):
            validate(outputs, config)

    def test_missing_params_raises(self) -> None:
        outputs = {"children": pa.table({"parent_id": ["1"]})}
        config: dict[str, Any] = {
            "validators": [{"name": "parent_window_respected", "params": {}}],
            "relationships": [],
        }
        with pytest.raises(ValueError, match="parent_window_respected"):
            validate(outputs, config)


class TestReconciliationHolds:
    def test_exact_sum_reconciliation_passes(self) -> None:
        outputs = {
            "orders": pa.table({"id": ["1"], "total": [30.0]}),
            "items": pa.table({"order_id": ["1", "1"], "amount": [10.0, 20.0]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "reconciliation_holds",
                    "params": {
                        "parent_table": "orders",
                        "parent_column": "total",
                        "child_table": "items",
                        "child_column": "amount",
                        "op": "sum",
                    },
                }
            ],
            "relationships": [_rel("orders", ["id"], "items", ["order_id"])],
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_perturbed_parent_total_caught(self) -> None:
        outputs = {
            "orders": pa.table({"id": ["1"], "total": [31.0]}),
            "items": pa.table({"order_id": ["1", "1"], "amount": [10.0, 20.0]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "reconciliation_holds",
                    "params": {
                        "parent_table": "orders",
                        "parent_column": "total",
                        "child_table": "items",
                        "child_column": "amount",
                        "op": "sum",
                    },
                }
            ],
            "relationships": [_rel("orders", ["id"], "items", ["order_id"])],
        }
        report = validate(outputs, config)
        assert report.passed is False
        assert report.findings[0].failing_row_indices == (0,)

    def test_tolerance_absorbs_sub_tolerance_delta(self) -> None:
        outputs = {
            "orders": pa.table({"id": ["1"], "total": [30.001]}),
            "items": pa.table({"order_id": ["1", "1"], "amount": [10.0, 20.0]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "reconciliation_holds",
                    "params": {
                        "parent_table": "orders",
                        "parent_column": "total",
                        "child_table": "items",
                        "child_column": "amount",
                        "op": "sum",
                        "tolerance": 0.01,
                    },
                }
            ],
            "relationships": [_rel("orders", ["id"], "items", ["order_id"])],
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_count_op(self) -> None:
        outputs = {
            "orders": pa.table({"id": ["1"], "item_count": [2]}),
            "items": pa.table({"order_id": ["1", "1"]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "reconciliation_holds",
                    "params": {
                        "parent_table": "orders",
                        "parent_column": "item_count",
                        "child_table": "items",
                        "child_column": "order_id",
                        "op": "count",
                    },
                }
            ],
            "relationships": [_rel("orders", ["id"], "items", ["order_id"])],
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_missing_relationship_edge_raises(self) -> None:
        outputs = {
            "orders": pa.table({"id": ["1"], "total": [30.0]}),
            "items": pa.table({"order_id": ["1"], "amount": [30.0]}),
        }
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "reconciliation_holds",
                    "params": {
                        "parent_table": "orders",
                        "parent_column": "total",
                        "child_table": "items",
                        "child_column": "amount",
                    },
                }
            ],
            "relationships": [],
        }
        with pytest.raises(ValueError, match="reconciliation_holds"):
            validate(outputs, config)
