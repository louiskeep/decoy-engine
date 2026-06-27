"""Unit tests for the closed-vocabulary Lark expression parser.

TDD: tests written before the implementation so every acceptance
criterion has a failing test before it has a passing one. Covers:
  - per-operator happy paths
  - REJECTION tests that must raise ValidationError
  - performance budget (compile once + evaluate 10k rows)
"""
from __future__ import annotations

import time

import pytest

from decoy_engine.errors import ValidationError


def _get_parser():
    from decoy_engine.expressions import compile_expr, evaluate
    return compile_expr, evaluate


class TestArithmeticOperators:
    def test_add(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a + b")
        assert evaluate(c, {"a": 3, "b": 4}) == 7

    def test_subtract(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a - b")
        assert evaluate(c, {"a": 10, "b": 3}) == 7

    def test_multiply(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a * b")
        assert evaluate(c, {"a": 6, "b": 7}) == 42

    def test_divide(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a / b")
        assert abs(evaluate(c, {"a": 10, "b": 4}) - 2.5) < 1e-9

    def test_floor_divide(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a // b")
        assert evaluate(c, {"a": 10, "b": 3}) == 3

    def test_unary_negation(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("-a")
        assert evaluate(c, {"a": 5}) == -5

    def test_complex_arithmetic(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a * b + c")
        assert evaluate(c, {"a": 2, "b": 3, "c": 1}) == 7


class TestComparisonOperators:
    def test_eq(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a == b")
        assert evaluate(c, {"a": 1, "b": 1}) is True
        assert evaluate(c, {"a": 1, "b": 2}) is False

    def test_ne(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a != b")
        assert evaluate(c, {"a": 1, "b": 2}) is True

    def test_lt(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a < b")
        assert evaluate(c, {"a": 1, "b": 2}) is True
        assert evaluate(c, {"a": 2, "b": 1}) is False

    def test_gt(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a > b")
        assert evaluate(c, {"a": 5, "b": 3}) is True

    def test_lte(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a <= b")
        assert evaluate(c, {"a": 3, "b": 3}) is True
        assert evaluate(c, {"a": 4, "b": 3}) is False

    def test_gte(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a >= b")
        assert evaluate(c, {"a": 3, "b": 3}) is True

    def test_in_membership(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr('status in ["active", "pending"]')
        assert evaluate(c, {"status": "active"}) is True
        assert evaluate(c, {"status": "inactive"}) is False


class TestLogicalOperators:
    def test_and_true(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a and b")
        assert evaluate(c, {"a": True, "b": True}) is True

    def test_and_false(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a and b")
        assert evaluate(c, {"a": True, "b": False}) is False

    def test_or_true(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a or b")
        assert evaluate(c, {"a": False, "b": True}) is True

    def test_or_false(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a or b")
        assert evaluate(c, {"a": False, "b": False}) is False

    def test_not_true(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("not a")
        assert evaluate(c, {"a": False}) is True

    def test_not_false(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("not a")
        assert evaluate(c, {"a": True}) is False


class TestStringConcat:
    def test_concat_two_strings(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("concat(first_name, last_name)")
        assert evaluate(c, {"first_name": "Jane", "last_name": "Doe"}) == "JaneDoe"

    def test_concat_with_literal(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr('concat(first_name, " Smith")')
        assert evaluate(c, {"first_name": "Jane"}) == "Jane Smith"


class TestDaysBetween:
    def test_days_between_dates(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("days_between(start_date, end_date)")
        result = evaluate(c, {"start_date": "2024-01-01", "end_date": "2024-01-11"})
        assert result == 10

    def test_days_between_negative(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("days_between(start_date, end_date)")
        result = evaluate(c, {"start_date": "2024-01-11", "end_date": "2024-01-01"})
        assert result == -10


class TestTernary:
    def test_ternary_true_branch(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("1 if flag else 0")
        assert evaluate(c, {"flag": True}) == 1
        assert evaluate(c, {"flag": False}) == 0

    def test_ternary_with_column_ref(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("adult_rate if age >= 18 else child_rate")
        assert evaluate(c, {"age": 25, "adult_rate": 10.0, "child_rate": 5.0}) == 10.0
        assert evaluate(c, {"age": 10, "adult_rate": 10.0, "child_rate": 5.0}) == 5.0

    def test_nested_ternary(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("1 if a > 10 else 2 if a > 5 else 3")
        assert evaluate(c, {"a": 15}) == 1
        assert evaluate(c, {"a": 7}) == 2
        assert evaluate(c, {"a": 3}) == 3


class TestColumnReference:
    def test_simple_column_ref(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("my_col")
        assert evaluate(c, {"my_col": 42}) == 42

    def test_column_ref_missing_raises(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("missing_col")
        with pytest.raises((KeyError, ValidationError)):
            evaluate(c, {})

    def test_numeric_literal(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("42")
        assert evaluate(c, {}) == 42

    def test_float_literal(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("3.14")
        assert abs(evaluate(c, {}) - 3.14) < 1e-9

    def test_string_literal(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr('"hello"')
        assert evaluate(c, {}) == "hello"

    def test_true_literal(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("True")
        assert evaluate(c, {}) is True

    def test_false_literal(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("False")
        assert evaluate(c, {}) is False

    def test_none_literal(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("None")
        assert evaluate(c, {}) is None


class TestRejection:
    """SECURITY: these expressions MUST raise ValidationError.

    The closed grammar provides the security boundary. Anything outside
    the grammar (arbitrary function calls, attribute access, dunder access,
    Python builtins) is a parse-time rejection, not a runtime guard.
    """

    def test_rejects_eval_call(self):
        compile_expr, evaluate = _get_parser()
        with pytest.raises(ValidationError, match="unsupported"):
            compile_expr("eval('1+1')")

    def test_rejects_import_call(self):
        compile_expr, evaluate = _get_parser()
        with pytest.raises(ValidationError, match="unsupported"):
            compile_expr("__import__('os')")

    def test_rejects_attribute_access(self):
        compile_expr, evaluate = _get_parser()
        with pytest.raises(ValidationError, match="unsupported"):
            compile_expr("x.__class__")

    def test_rejects_dunder_identifier(self):
        compile_expr, evaluate = _get_parser()
        with pytest.raises(ValidationError, match="unsupported"):
            compile_expr("__builtins__")

    def test_rejects_arbitrary_function_call(self):
        compile_expr, evaluate = _get_parser()
        with pytest.raises(ValidationError, match="unsupported"):
            compile_expr("open('/etc/passwd')")

    def test_rejects_getattr_call(self):
        compile_expr, evaluate = _get_parser()
        with pytest.raises(ValidationError, match="unsupported"):
            compile_expr("getattr(x, 'y')")

    def test_rejects_exec_call(self):
        compile_expr, evaluate = _get_parser()
        with pytest.raises(ValidationError, match="unsupported"):
            compile_expr("exec('import os')")

    def test_rejects_lambda(self):
        compile_expr, evaluate = _get_parser()
        with pytest.raises(ValidationError, match="unsupported"):
            compile_expr("lambda x: x")

    def test_rejects_subscript_on_col(self):
        compile_expr, evaluate = _get_parser()
        with pytest.raises(ValidationError, match="unsupported"):
            compile_expr('col["key"]')


class TestCompileOnce:
    """compile_expr produces a CompiledExpression object."""

    def test_compiled_expression_is_reusable(self):
        compile_expr, evaluate = _get_parser()
        c = compile_expr("a + b")
        assert evaluate(c, {"a": 1, "b": 2}) == 3
        assert evaluate(c, {"a": 10, "b": 20}) == 30


@pytest.mark.perf
class TestPerformance:
    """SP-06 perf budget: compile once + evaluate 10k rows in under 5s."""

    def test_compile_once_evaluate_10k(self):
        compile_expr, evaluate = _get_parser()
        expr = "a * b + c"
        c = compile_expr(expr)

        start = time.monotonic()
        for i in range(10_000):
            evaluate(c, {"a": i, "b": 2, "c": 1})
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, f"10k evaluations took {elapsed:.2f}s (budget: 5s)"
