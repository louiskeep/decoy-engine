"""Sprint 6: SQL injection surface hardening tests.

Covers:
- _validate_sql_identifier: valid and invalid inputs.
- source_db / target_db validate_config: identifier injection rejection.
- filter / if_router / derive Polars paths: pl.sql_expr() in use,
  SQLContext absent, and correct functional output.
"""

import inspect

import polars as pl
import pytest

from decoy_engine.graph.ops import derive, filter_op, if_router
from decoy_engine.graph.ops._base import OpError
from decoy_engine.graph.ops.source_db import (
    _validate_sql_identifier,
)
from decoy_engine.graph.ops.source_db import (
    validate_config as source_validate,
)
from decoy_engine.graph.ops.target_db import validate_config as target_validate
from decoy_engine.internal.validator import ValidationError

# ---------------------------------------------------------------------------
# _validate_sql_identifier
# ---------------------------------------------------------------------------


class TestValidateSqlIdentifier:
    @pytest.mark.parametrize(
        "name",
        [
            "users",
            "my_table",
            "MySchema",
            "table123",
            "_private",
            "col$name",
            "A",
        ],
    )
    def test_valid_identifiers(self, name):
        _validate_sql_identifier(name, "config.table")  # must not raise

    @pytest.mark.parametrize(
        "name",
        [
            'table"with_quote',
            "table;drop",
            "my table",
            "schema.table",
            "table--comment",
            "table'quote",
            "123starts_digit",
            "",
        ],
    )
    def test_invalid_identifiers_raise(self, name):
        with pytest.raises(ValidationError, match="disallowed characters"):
            _validate_sql_identifier(name, "config.table")


# ---------------------------------------------------------------------------
# source_db.validate_config -- identifier gates
# ---------------------------------------------------------------------------


class TestSourceDbIdentifierValidation:
    def _cfg(self, **kw):
        return {"table": "users", "dsn": "sqlite:///test.db", **kw}

    def test_valid_table_passes(self):
        source_validate(self._cfg())

    def test_valid_schema_passes(self):
        source_validate(self._cfg(schema="public"))

    def test_table_with_quote_raises(self):
        with pytest.raises(ValidationError, match="disallowed characters"):
            source_validate(self._cfg(table='users"DROP'))

    def test_table_with_semicolon_raises(self):
        with pytest.raises(ValidationError, match="disallowed characters"):
            source_validate(self._cfg(table="users;DROP TABLE users"))

    def test_schema_with_quote_raises(self):
        with pytest.raises(ValidationError, match="disallowed characters"):
            source_validate(self._cfg(schema='myschema"DROP'))

    def test_table_with_space_raises(self):
        with pytest.raises(ValidationError, match="disallowed characters"):
            source_validate(self._cfg(table="my table"))


# ---------------------------------------------------------------------------
# target_db.validate_config -- identifier gates
# ---------------------------------------------------------------------------


class TestTargetDbIdentifierValidation:
    def _cfg(self, **kw):
        return {"table": "results", "dsn": "sqlite:///test.db", **kw}

    def test_valid_table_passes(self):
        target_validate(self._cfg())

    def test_valid_schema_passes(self):
        target_validate(self._cfg(schema="dbo"))

    def test_table_injection_raises(self):
        with pytest.raises(ValidationError, match="disallowed characters"):
            target_validate(self._cfg(table='output"DROP TABLE results'))

    def test_schema_injection_raises(self):
        with pytest.raises(ValidationError, match="disallowed characters"):
            target_validate(self._cfg(schema='myschema"--'))

    def test_table_with_semicolon_raises(self):
        with pytest.raises(ValidationError, match="disallowed characters"):
            target_validate(self._cfg(table="results; DROP TABLE results"))


# ---------------------------------------------------------------------------
# filter -- Polars pl.sql_expr() path
# ---------------------------------------------------------------------------


class TestFilterPolarsExprAPI:
    def test_no_sqlcontext_in_apply_polars_source(self):
        """_apply_polars must not contain SQLContext (regression guard)."""
        src = inspect.getsource(filter_op._apply_polars)
        assert "SQLContext" not in src

    def test_basic_numeric_filter(self):
        df = pl.DataFrame({"age": [10, 20, 30], "name": ["a", "b", "c"]})
        result = filter_op.apply([df], {"predicate": "age >= 20"}, None)
        assert len(result) == 2
        assert result["name"].to_list() == ["b", "c"]

    def test_compound_predicate(self):
        df = pl.DataFrame({"age": [10, 20, 30], "score": [5, 10, 3]})
        result = filter_op.apply([df], {"predicate": "age >= 20 AND score >= 10"}, None)
        assert len(result) == 1
        assert result["age"].to_list() == [20]

    def test_string_equality(self):
        df = pl.DataFrame({"state": ["CA", "NY", "CA", "TX"]})
        result = filter_op.apply([df], {"predicate": "state = 'CA'"}, None)
        assert len(result) == 2

    def test_all_filtered_out(self):
        df = pl.DataFrame({"x": [1, 2, 3]})
        result = filter_op.apply([df], {"predicate": "x > 100"}, None)
        assert len(result) == 0

    def test_none_filtered_out(self):
        df = pl.DataFrame({"x": [1, 2, 3]})
        result = filter_op.apply([df], {"predicate": "x > 0"}, None)
        assert len(result) == 3

    def test_missing_column_raises_op_error(self):
        df = pl.DataFrame({"x": [1]})
        with pytest.raises(OpError):
            filter_op.apply([df], {"predicate": "missing_col > 0"}, None)


# ---------------------------------------------------------------------------
# if_router -- Polars pl.sql_expr() path
# ---------------------------------------------------------------------------


class TestIfRouterPolarsExprAPI:
    def test_no_sqlcontext_in_apply_source(self):
        """apply() must not contain SQLContext (regression guard)."""
        src = inspect.getsource(if_router.apply)
        assert "SQLContext" not in src

    def test_basic_numeric_split(self):
        df = pl.DataFrame({"v": [1, 5, 10, 15]})
        result = if_router.apply([df], {"predicate": "v >= 10"}, None)
        assert len(result["pass"]) == 2
        assert len(result["fail"]) == 2

    def test_pass_plus_fail_equals_total(self):
        df = pl.DataFrame({"v": list(range(20))})
        result = if_router.apply([df], {"predicate": "v < 10"}, None)
        assert len(result["pass"]) + len(result["fail"]) == 20

    def test_string_predicate_negation(self):
        df = pl.DataFrame({"s": ["a", "b", "c"]})
        result = if_router.apply([df], {"predicate": "s = 'a'"}, None)
        assert result["pass"]["s"].to_list() == ["a"]
        assert result["fail"]["s"].to_list() == ["b", "c"]

    def test_all_pass(self):
        df = pl.DataFrame({"x": [1, 2, 3]})
        result = if_router.apply([df], {"predicate": "x > 0"}, None)
        assert len(result["pass"]) == 3
        assert len(result["fail"]) == 0

    def test_all_fail(self):
        df = pl.DataFrame({"x": [1, 2, 3]})
        result = if_router.apply([df], {"predicate": "x > 100"}, None)
        assert len(result["pass"]) == 0
        assert len(result["fail"]) == 3

    def test_missing_column_raises_op_error(self):
        df = pl.DataFrame({"x": [1]})
        with pytest.raises(OpError):
            if_router.apply([df], {"predicate": "missing_col > 0"}, None)


# ---------------------------------------------------------------------------
# derive -- Polars pl.sql_expr() path
# ---------------------------------------------------------------------------


class TestDerivePolarsExprAPI:
    def test_no_sqlcontext_in_apply_polars_source(self):
        """_apply_polars must not contain SQLContext (regression guard)."""
        src = inspect.getsource(derive._apply_polars)
        assert "SQLContext" not in src

    def test_arithmetic_expression(self):
        df = pl.DataFrame({"score": [10, 20, 30]})
        result = derive.apply([df], {"column": "doubled", "expression": "score * 2"}, None)
        assert result["doubled"].to_list() == [20, 40, 60]

    def test_constant_expression(self):
        df = pl.DataFrame({"x": [1, 2, 3]})
        result = derive.apply([df], {"column": "tag", "expression": "1"}, None)
        assert result["tag"].to_list() == [1, 1, 1]

    def test_multi_column_expression(self):
        df = pl.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        result = derive.apply([df], {"column": "sum_ab", "expression": "a + b"}, None)
        assert result["sum_ab"].to_list() == [5, 7, 9]

    def test_overwrites_existing_column(self):
        df = pl.DataFrame({"score": [10, 20, 30]})
        result = derive.apply([df], {"column": "score", "expression": "score + 1"}, None)
        assert result["score"].to_list() == [11, 21, 31]
        # original frame must not be mutated
        assert df["score"].to_list() == [10, 20, 30]

    def test_all_original_columns_preserved(self):
        df = pl.DataFrame({"a": [1], "b": [2]})
        result = derive.apply([df], {"column": "c", "expression": "a + b"}, None)
        assert set(result.columns) == {"a", "b", "c"}

    def test_missing_column_raises_op_error(self):
        df = pl.DataFrame({"x": [1]})
        with pytest.raises(OpError, match="derive expression failed"):
            derive.apply([df], {"column": "y", "expression": "no_such_col + 1"}, None)
