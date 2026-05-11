"""Tests for the sql_run graph op (Sprint G Week 5).

Covers config validation, single-input SELECT execution, aggregate
queries, predicate filters, error mapping for bad SQL, and OPS registry
plumbing. The op runs DuckDB SQL against a pyarrow.Table; tests use
pa.table fixtures so they don't depend on the runner's Arrow boundary
materialization.
"""
from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.graph.ops import OPS, sql_run
from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError


def _table_from_dict(data: dict[str, list]) -> pa.Table:
    return pa.table(data)


class TestValidation:
    def test_missing_sql_rejected(self):
        with pytest.raises(ValidationError, match="sql"):
            sql_run.validate_config({})

    def test_empty_string_sql_rejected(self):
        with pytest.raises(ValidationError, match="sql"):
            sql_run.validate_config({"sql": ""})

    def test_whitespace_only_sql_rejected(self):
        with pytest.raises(ValidationError, match="sql"):
            sql_run.validate_config({"sql": "   \n  "})

    def test_non_string_sql_rejected(self):
        with pytest.raises(ValidationError, match="sql"):
            sql_run.validate_config({"sql": 42})

    def test_valid_select_passes(self):
        sql_run.validate_config({"sql": "SELECT * FROM df"})


class TestApply:
    def test_select_star_passes_through(self):
        df = _table_from_dict({"id": [1, 2, 3], "name": ["a", "b", "c"]})
        out = sql_run.apply(
            inputs=[df],
            config={"sql": "SELECT * FROM df"},
            ctx=None,
        )
        assert isinstance(out, pa.Table)
        assert out.num_rows == 3
        assert out.column_names == ["id", "name"]
        assert out.column("name").to_pylist() == ["a", "b", "c"]

    def test_filter_predicate(self):
        df = _table_from_dict({"id": [1, 2, 3, 4], "state": ["KY", "TN", "KY", "OH"]})
        out = sql_run.apply(
            inputs=[df],
            config={"sql": "SELECT * FROM df WHERE state = 'KY'"},
            ctx=None,
        )
        assert out.num_rows == 2
        assert out.column("id").to_pylist() == [1, 3]

    def test_projection_and_alias(self):
        df = _table_from_dict({"a": [1, 2, 3], "b": [10, 20, 30]})
        out = sql_run.apply(
            inputs=[df],
            config={"sql": "SELECT a, b, a + b AS total FROM df"},
            ctx=None,
        )
        assert out.column_names == ["a", "b", "total"]
        assert out.column("total").to_pylist() == [11, 22, 33]

    def test_aggregate(self):
        df = _table_from_dict({"category": ["x", "y", "x", "y", "x"], "value": [1, 2, 3, 4, 5]})
        out = sql_run.apply(
            inputs=[df],
            config={
                "sql": (
                    "SELECT category, SUM(value) AS total "
                    "FROM df GROUP BY category ORDER BY category"
                ),
            },
            ctx=None,
        )
        assert out.num_rows == 2
        assert out.column("category").to_pylist() == ["x", "y"]
        assert out.column("total").to_pylist() == [9, 6]

    def test_window_function(self):
        df = _table_from_dict({"id": [1, 2, 3, 4], "value": [10, 20, 30, 40]})
        out = sql_run.apply(
            inputs=[df],
            config={
                "sql": (
                    "SELECT id, value, "
                    "SUM(value) OVER (ORDER BY id) AS running_sum "
                    "FROM df"
                ),
            },
            ctx=None,
        )
        assert out.column("running_sum").to_pylist() == [10, 30, 60, 100]


class TestErrors:
    def test_invalid_sql_raises_op_error(self):
        df = _table_from_dict({"id": [1]})
        with pytest.raises(OpError, match="SQL execution failed"):
            sql_run.apply(
                inputs=[df],
                config={"sql": "SELECT * FROM nonexistent_table"},
                ctx=None,
            )

    def test_syntax_error_raises_op_error(self):
        df = _table_from_dict({"id": [1]})
        with pytest.raises(OpError, match="SQL execution failed"):
            sql_run.apply(
                inputs=[df],
                config={"sql": "SELEC * FRM df"},
                ctx=None,
            )

    def test_referencing_missing_column_raises_op_error(self):
        df = _table_from_dict({"id": [1]})
        with pytest.raises(OpError, match="SQL execution failed"):
            sql_run.apply(
                inputs=[df],
                config={"sql": "SELECT no_such_column FROM df"},
                ctx=None,
            )


class TestRegistry:
    def test_registered_in_ops_dispatch(self):
        assert "sql_run" in OPS
        op = OPS["sql_run"]
        assert op.KIND == "sql_run"
        assert op.NATIVE_ENGINE == "duckdb"
        assert op.INPUT_ARITY == (1, 1)
        assert op.OUTPUT_KIND == "stream"
