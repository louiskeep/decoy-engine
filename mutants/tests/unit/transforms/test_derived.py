"""Unit tests for the derived column-level strategy transform (SP-10 TDD).

Written BEFORE the implementation per the TDD contract (testing.md).
Tests cover:
  - Per-operator integration through the transform
  - null_propagation per mode (explicit_null, sentinel, default)
  - Bounds enforcement (clip within [min, max])
  - Cyclic reference rejection
  - Missing column ref rejection
  - Determinism: same row context -> same output
"""

from __future__ import annotations

import pytest

from decoy_engine.plan._errors import PlanCompileError


class TestDerivedConfigParsing:
    """DerivedConfig.from_dict validates expression syntax at config-parse time."""

    def test_valid_expression_accepted(self) -> None:
        from decoy_engine.transforms.derived import DerivedConfig

        cfg = DerivedConfig.from_dict({"expression": "a + b"})
        assert cfg.expression == "a + b"

    def test_invalid_expression_raises_validation_error(self) -> None:
        from decoy_engine.errors import ValidationError
        from decoy_engine.transforms.derived import DerivedConfig

        with pytest.raises(ValidationError):
            DerivedConfig.from_dict({"expression": "import os"})

    def test_missing_expression_raises_plan_compile_error(self) -> None:
        from decoy_engine.transforms.derived import DerivedConfig

        with pytest.raises(PlanCompileError, match="expression"):
            DerivedConfig.from_dict({})

    def test_bounds_parsed(self) -> None:
        from decoy_engine.transforms.derived import DerivedConfig

        cfg = DerivedConfig.from_dict({"expression": "a + 1", "bounds": {"min": 0, "max": 100}})
        assert cfg.bounds is not None
        assert cfg.bounds["min"] == 0
        assert cfg.bounds["max"] == 100

    def test_null_propagation_default_is_explicit_null(self) -> None:
        from decoy_engine.transforms.derived import DerivedConfig

        cfg = DerivedConfig.from_dict({"expression": "a + 1"})
        assert cfg.null_propagation == "explicit_null"

    def test_null_propagation_modes_accepted(self) -> None:
        from decoy_engine.transforms.derived import DerivedConfig

        for mode in ("explicit_null", "sentinel", "default"):
            cfg = DerivedConfig.from_dict({"expression": "a + 1", "null_propagation": mode})
            assert cfg.null_propagation == mode

    def test_invalid_null_propagation_raises(self) -> None:
        from decoy_engine.transforms.derived import DerivedConfig

        with pytest.raises(PlanCompileError, match="null_propagation"):
            DerivedConfig.from_dict({"expression": "a + 1", "null_propagation": "unknown_mode"})


class TestApplyDerivedPerOperator:
    """Exercise each Lark operator through apply_derived (integration of SP-06 parser)."""

    def _apply(self, expression: str, row_context: dict) -> object:
        from decoy_engine.transforms.derived import DerivedConfig, apply_derived

        cfg = DerivedConfig.from_dict({"expression": expression})
        return apply_derived(cfg, row_context)

    def test_arithmetic_addition(self) -> None:
        assert self._apply("a + b", {"a": 3, "b": 4}) == 7

    def test_arithmetic_subtraction(self) -> None:
        assert self._apply("a - b", {"a": 10, "b": 3}) == 7

    def test_arithmetic_multiplication(self) -> None:
        assert self._apply("a * b", {"a": 3, "b": 4}) == 12

    def test_arithmetic_division(self) -> None:
        result = self._apply("a / b", {"a": 10, "b": 4})
        assert abs(result - 2.5) < 1e-9

    def test_arithmetic_floor_division(self) -> None:
        assert self._apply("a // b", {"a": 10, "b": 3}) == 3

    def test_comparison_eq(self) -> None:
        assert self._apply("a == b", {"a": 5, "b": 5}) is True
        assert self._apply("a == b", {"a": 5, "b": 6}) is False

    def test_comparison_neq(self) -> None:
        assert self._apply("a != b", {"a": 5, "b": 6}) is True

    def test_comparison_lt(self) -> None:
        assert self._apply("a < b", {"a": 3, "b": 5}) is True

    def test_comparison_gt(self) -> None:
        assert self._apply("a > b", {"a": 5, "b": 3}) is True

    def test_comparison_le(self) -> None:
        assert self._apply("a <= b", {"a": 3, "b": 3}) is True

    def test_comparison_ge(self) -> None:
        assert self._apply("a >= b", {"a": 5, "b": 3}) is True

    def test_comparison_in(self) -> None:
        assert self._apply("a in b", {"a": 2, "b": [1, 2, 3]}) is True
        assert self._apply("a in b", {"a": 9, "b": [1, 2, 3]}) is False

    def test_logical_and(self) -> None:
        assert self._apply("a and b", {"a": True, "b": True}) is True
        assert self._apply("a and b", {"a": True, "b": False}) is False

    def test_logical_or(self) -> None:
        assert self._apply("a or b", {"a": False, "b": True}) is True

    def test_logical_not(self) -> None:
        assert self._apply("not a", {"a": False}) is True

    def test_concat_function(self) -> None:
        result = self._apply("concat(a, b)", {"a": "hello", "b": "world"})
        assert result == "helloworld"

    def test_concat_function_n_arg(self) -> None:
        result = self._apply("concat(a, b, c)", {"a": "x", "b": "-", "c": "y"})
        assert result == "x-y"

    def test_slice_last4(self) -> None:
        """The HC-6 motivating case: last4(firstname) via slice(firstname, -4)."""
        result = self._apply("slice(firstname, -4)", {"firstname": "Alexander"})
        assert result == "nder"

    def test_slice_first_n(self) -> None:
        result = self._apply("slice(firstname, 0, 4)", {"firstname": "Alexander"})
        assert result == "Alex"

    def test_slice_middle(self) -> None:
        result = self._apply("slice(firstname, 2, 5)", {"firstname": "Alexander"})
        assert result == "exa"

    def test_slice_two_arg_form(self) -> None:
        result = self._apply("slice(s, 3)", {"s": "abcdef"})
        assert result == "def"

    def test_slice_out_of_range_clamps(self) -> None:
        result = self._apply("slice(s, 0, 100)", {"s": "abc"})
        assert result == "abc"

    def test_ternary_if_else(self) -> None:
        result = self._apply("a if a > 0 else b", {"a": 5, "b": -1})
        assert result == 5
        result2 = self._apply("a if a > 0 else b", {"a": -1, "b": 99})
        assert result2 == 99

    def test_numeric_literal(self) -> None:
        assert self._apply("a + 10", {"a": 5}) == 15

    def test_string_literal(self) -> None:
        result = self._apply('concat(a, "!")', {"a": "hello"})
        assert result == "hello!"

    def test_bool_literal(self) -> None:
        assert self._apply("True", {}) is True
        assert self._apply("False", {}) is False

    def test_none_literal(self) -> None:
        assert self._apply("None", {}) is None

    def test_paren_grouping(self) -> None:
        assert self._apply("(a + b) * c", {"a": 2, "b": 3, "c": 4}) == 20

    def test_unary_negation(self) -> None:
        assert self._apply("-a", {"a": 5}) == -5


class TestNullPropagation:
    """Null propagation modes behave correctly."""

    def _apply(self, expression: str, row_context: dict, *, null_propagation: str) -> object:
        from decoy_engine.transforms.derived import DerivedConfig, apply_derived

        cfg = DerivedConfig.from_dict(
            {"expression": expression, "null_propagation": null_propagation}
        )
        return apply_derived(cfg, row_context)

    def test_explicit_null_propagates_when_any_ref_is_none(self) -> None:
        result = self._apply("a + b", {"a": None, "b": 5}, null_propagation="explicit_null")
        assert result is None

    def test_explicit_null_propagates_when_all_refs_none(self) -> None:
        result = self._apply("a + b", {"a": None, "b": None}, null_propagation="explicit_null")
        assert result is None

    def test_explicit_null_does_not_propagate_when_no_null(self) -> None:
        result = self._apply("a + b", {"a": 3, "b": 4}, null_propagation="explicit_null")
        assert result == 7

    def test_sentinel_mode_replaces_none_with_empty_string(self) -> None:
        # concat("hello", None) -> concat("hello", "") -> "hello"
        result = self._apply("concat(a, b)", {"a": "hello", "b": None}, null_propagation="sentinel")
        assert result == "hello"

    def test_sentinel_mode_runs_expression(self) -> None:
        # a=None treated as "", expression still runs
        result = self._apply('concat(a, "!")', {"a": None}, null_propagation="sentinel")
        assert result == "!"

    def test_default_mode_coerces_none_to_zero(self) -> None:
        # a=None coerced to 0; 0 + 5 = 5
        result = self._apply("a + b", {"a": None, "b": 5}, null_propagation="default")
        assert result == 5

    def test_default_mode_non_null_values_unchanged(self) -> None:
        result = self._apply("a + b", {"a": 3, "b": 4}, null_propagation="default")
        assert result == 7

    def test_explicit_null_propagates_over_slice_subject(self) -> None:
        """A null column feeding slice()'s subject obeys explicit_null mode."""
        result = self._apply("slice(a, -4)", {"a": None}, null_propagation="explicit_null")
        assert result is None

    def test_sentinel_mode_slice_over_null_subject(self) -> None:
        # a=None -> "" (sentinel); slice("", -4) -> "" (clamps, no error)
        result = self._apply("slice(a, -4)", {"a": None}, null_propagation="sentinel")
        assert result == ""


class TestBoundsEnforcement:
    """Bounds clip numeric output; non-numeric output is not clipped."""

    def _apply(self, expression: str, row_context: dict, *, bounds: dict) -> object:
        from decoy_engine.transforms.derived import DerivedConfig, apply_derived

        cfg = DerivedConfig.from_dict({"expression": expression, "bounds": bounds})
        return apply_derived(cfg, row_context)

    def test_value_above_max_is_clipped_to_max(self) -> None:
        result = self._apply("a", {"a": 150}, bounds={"min": 0, "max": 100})
        assert result == 100

    def test_value_below_min_is_clipped_to_min(self) -> None:
        result = self._apply("a", {"a": -10}, bounds={"min": 0, "max": 100})
        assert result == 0

    def test_value_within_bounds_is_unchanged(self) -> None:
        result = self._apply("a", {"a": 50}, bounds={"min": 0, "max": 100})
        assert result == 50

    def test_value_at_boundary_is_unchanged(self) -> None:
        result = self._apply("a", {"a": 0}, bounds={"min": 0, "max": 100})
        assert result == 0
        result2 = self._apply("a", {"a": 100}, bounds={"min": 0, "max": 100})
        assert result2 == 100

    def test_non_numeric_result_ignores_bounds(self) -> None:
        result = self._apply('"hello"', {}, bounds={"min": 0, "max": 10})
        assert result == "hello"

    def test_none_result_ignores_bounds(self) -> None:
        result = self._apply("None", {}, bounds={"min": 0, "max": 10})
        assert result is None

    def test_bounds_min_greater_than_max_raises(self) -> None:
        """M1: bounds with min > max must be rejected at config-parse time."""
        from decoy_engine.transforms.derived import DerivedConfig

        with pytest.raises(PlanCompileError, match="min"):
            DerivedConfig.from_dict({"expression": "a", "bounds": {"min": 100, "max": 0}})

    def test_bounds_min_equal_max_is_accepted(self) -> None:
        """Degenerate min==max is valid (clamps to a single value)."""
        from decoy_engine.transforms.derived import DerivedConfig

        cfg = DerivedConfig.from_dict({"expression": "a", "bounds": {"min": 5, "max": 5}})
        assert cfg.bounds is not None
        assert cfg.bounds["min"] == 5.0
        assert cfg.bounds["max"] == 5.0


class TestPerRowEvalError:
    """M2: per-row evaluation errors must carry column context."""

    def test_div_by_zero_names_column(self) -> None:
        """A ZeroDivisionError during row evaluation must name the column."""
        from decoy_engine.transforms.derived import DerivedConfig, apply_derived

        cfg = DerivedConfig.from_dict({"expression": "a / b", "null_propagation": "default"})
        with pytest.raises(ValueError, match="computed_col"):
            apply_derived(cfg, {"a": 1, "b": 0}, column="computed_col", row_index=0)

    def test_eval_error_includes_row_index(self) -> None:
        """Row index appears in the error message when provided."""
        from decoy_engine.transforms.derived import DerivedConfig, apply_derived

        cfg = DerivedConfig.from_dict({"expression": "a / b", "null_propagation": "default"})
        with pytest.raises(ValueError, match="7"):
            apply_derived(cfg, {"a": 1, "b": 0}, column="col", row_index=7)

    def test_eval_error_without_column_still_raises(self) -> None:
        """Errors raised with no column keyword still propagate (backward-compat)."""
        from decoy_engine.transforms.derived import DerivedConfig, apply_derived

        cfg = DerivedConfig.from_dict({"expression": "a / b", "null_propagation": "default"})
        with pytest.raises((ValueError, ZeroDivisionError)):
            apply_derived(cfg, {"a": 1, "b": 0})


class TestDeterminism:
    """Same row context always produces the same output (no RNG)."""

    def test_same_row_same_output(self) -> None:
        from decoy_engine.transforms.derived import DerivedConfig, apply_derived

        cfg = DerivedConfig.from_dict({"expression": "a + b * 2"})
        row = {"a": 3, "b": 7}
        result1 = apply_derived(cfg, row)
        result2 = apply_derived(cfg, row)
        assert result1 == result2

    def test_different_rows_can_differ(self) -> None:
        from decoy_engine.transforms.derived import DerivedConfig, apply_derived

        cfg = DerivedConfig.from_dict({"expression": "a + b"})
        assert apply_derived(cfg, {"a": 1, "b": 2}) != apply_derived(cfg, {"a": 10, "b": 20})


class TestColumnRefExtraction:
    """_get_column_refs extracts column names from a compiled expression."""

    def test_single_ref(self) -> None:
        from decoy_engine.expressions import compile_expr
        from decoy_engine.transforms.derived import _get_column_refs

        compiled = compile_expr("a + 1")
        refs = _get_column_refs(compiled)
        assert refs == frozenset({"a"})

    def test_multiple_refs(self) -> None:
        from decoy_engine.expressions import compile_expr
        from decoy_engine.transforms.derived import _get_column_refs

        compiled = compile_expr("a + b * c")
        refs = _get_column_refs(compiled)
        assert refs == frozenset({"a", "b", "c"})

    def test_no_refs_for_literal_expression(self) -> None:
        from decoy_engine.expressions import compile_expr
        from decoy_engine.transforms.derived import _get_column_refs

        compiled = compile_expr("1 + 2")
        refs = _get_column_refs(compiled)
        assert refs == frozenset()

    def test_slice_subject_arg_surfaces_as_col_ref(self) -> None:
        """slice()'s subject argument is a normal expr sub-tree, so its
        column ref must surface via _get_column_refs (HC-6 parity with
        concat/days_between)."""
        from decoy_engine.expressions import compile_expr
        from decoy_engine.transforms.derived import _get_column_refs

        compiled = compile_expr("slice(firstname, -4)")
        refs = _get_column_refs(compiled)
        assert refs == frozenset({"firstname"})

    def test_slice_start_end_col_refs_also_surface(self) -> None:
        from decoy_engine.expressions import compile_expr
        from decoy_engine.transforms.derived import _get_column_refs

        compiled = compile_expr("slice(s, start, end)")
        refs = _get_column_refs(compiled)
        assert refs == frozenset({"s", "start", "end"})


class TestCheckDerivedColumnRefs:
    """Plan-compile check for derived column references (missing + cyclic)."""

    def _check(self, config: dict) -> None:
        from decoy_engine.plan._checks import check_derived_column_refs

        check_derived_column_refs(config)

    def test_valid_config_passes(self) -> None:
        config = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "a", "strategy": "passthrough"},
                        {"name": "b", "strategy": "passthrough"},
                        {
                            "name": "c",
                            "strategy": "derived",
                            "provider_config": {"expression": "a + b"},
                        },
                    ],
                }
            ]
        }
        self._check(config)  # no error

    def test_missing_column_ref_raises(self) -> None:
        config = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "a", "strategy": "passthrough"},
                        {
                            "name": "b",
                            "strategy": "derived",
                            "provider_config": {"expression": "a + missing_col"},
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="missing_col"):
            self._check(config)

    def test_self_reference_raises(self) -> None:
        config = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "a", "strategy": "passthrough"},
                        {
                            "name": "b",
                            "strategy": "derived",
                            "provider_config": {"expression": "b + 1"},
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="cyclic"):
            self._check(config)

    def test_transitive_cycle_raises(self) -> None:
        # a_derived = b_derived + 1, b_derived = a_derived + 1 -> cycle
        config = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "a",
                            "strategy": "derived",
                            "provider_config": {"expression": "b + 1"},
                        },
                        {
                            "name": "b",
                            "strategy": "derived",
                            "provider_config": {"expression": "a + 1"},
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="cyclic"):
            self._check(config)

    def test_no_derived_columns_passes(self) -> None:
        config = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "a", "strategy": "passthrough"},
                    ],
                }
            ]
        }
        self._check(config)  # no error

    def test_empty_config_passes(self) -> None:
        self._check({})  # no error

    def test_generate_table_derived_refs_sibling_generate_column_passes(self) -> None:
        """L1: a derived column in a generate table referencing a sibling generate_column passes."""
        config = {
            "tables": [
                {
                    "name": "t",
                    "row_count": 3,
                    "generate_columns": [
                        {"name": "a", "type": "sequence", "start": 1},
                        {"name": "b", "type": "derived", "expression": "a + 10"},
                    ],
                }
            ]
        }
        self._check(config)  # no error

    def test_generate_table_derived_missing_ref_raises(self) -> None:
        """L1: a derived column in a generate table referencing a non-existent column raises."""
        config = {
            "tables": [
                {
                    "name": "t",
                    "row_count": 3,
                    "generate_columns": [
                        {"name": "a", "type": "sequence", "start": 1},
                        {"name": "b", "type": "derived", "expression": "a + missing_col"},
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="missing_col"):
            self._check(config)


class TestDerivedGeneratePath:
    """H1: derived column in a generate table is reachable and computes correctly."""

    def _run_generate(self, generate_columns: list, row_count: int = 3) -> dict:
        """Call generate_tables with the given generate_columns; return col->values."""
        from tests.unit._dps_helpers import compile_and_generate

        cfg = {
            "version": 1,
            "global_settings": {"seed": 0},
            "sources": {},
            "tables": [
                {
                    "name": "t",
                    "row_count": row_count,
                    "generate_columns": generate_columns,
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
        tbl = compile_and_generate(cfg)["t"]
        return {col: tbl.column(col).to_pylist() for col in tbl.column_names}

    def test_derived_generate_column_computes_from_sibling(self) -> None:
        """A derived column in a generate table reads already-generated siblings."""
        out = self._run_generate(
            [
                {"name": "prefix", "type": "categorical", "categories": ["ID"], "weights": [1]},
                {"name": "label", "type": "derived", "expression": 'concat(prefix, "_ok")'},
            ]
        )
        assert "label" in out
        assert out["label"] == ["ID_ok", "ID_ok", "ID_ok"]

    def test_derived_generate_numeric_arithmetic(self) -> None:
        """Derived column in generate table: arithmetic over categorical sibling."""
        out = self._run_generate(
            [
                {
                    "name": "x",
                    "type": "categorical",
                    "categories": [10, 20, 30],
                    "weights": [1, 0, 0],
                },
                {"name": "y", "type": "derived", "expression": "x * 2"},
            ]
        )
        # x is always 10 (weight=[1,0,0]); y = 10*2 = 20
        assert out["y"] == [20, 20, 20]

    def test_derived_generate_concat_expression(self) -> None:
        """Derived column in generate table: string concat expression."""
        out = self._run_generate(
            [
                {"name": "prefix", "type": "categorical", "categories": ["hello"], "weights": [1]},
                {"name": "greeting", "type": "derived", "expression": 'concat(prefix, "!")'},
            ]
        )
        assert out["greeting"] == ["hello!", "hello!", "hello!"]

    def test_derived_generate_slice_last4_expression(self) -> None:
        """HC-6 mask/generate parity: last4(firstname) works identically in
        the generate path (the mask-path equivalent is covered by
        TestApplyDerivedPerOperator.test_slice_last4 and the adapter
        integration test in test_derived_strategy.py)."""
        out = self._run_generate(
            [
                {
                    "name": "firstname",
                    "type": "categorical",
                    "categories": ["Alexander"],
                    "weights": [1],
                },
                {"name": "last4", "type": "derived", "expression": "slice(firstname, -4)"},
            ]
        )
        assert out["last4"] == ["nder", "nder", "nder"]

    def test_derived_generate_ternary_expression(self) -> None:
        """Derived column in generate table: ternary expression over sibling."""
        out = self._run_generate(
            [
                {"name": "score", "type": "categorical", "categories": [80, 45], "weights": [1, 0]},
                {
                    "name": "grade",
                    "type": "derived",
                    "expression": '"pass" if score >= 50 else "fail"',
                },
            ]
        )
        assert out["grade"] == ["pass", "pass", "pass"]

    def test_derived_generate_three_rows_correct_values(self) -> None:
        """Integration: generate table with sequence + derived column, 3 rows."""
        from tests.unit._dps_helpers import compile_and_generate

        cfg = {
            "version": 1,
            "global_settings": {"seed": 0},
            "sources": {},
            "tables": [
                {
                    "name": "scores",
                    "row_count": 3,
                    "generate_columns": [
                        {
                            "name": "base",
                            "type": "categorical",
                            "categories": [5, 10, 15],
                            "weights": [1, 0, 0],
                        },
                        {"name": "doubled", "type": "derived", "expression": "base * 2"},
                        {"name": "tripled", "type": "derived", "expression": "base * 3"},
                    ],
                }
            ],
            "targets": {"scores": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
        tbl = compile_and_generate(cfg)["scores"]
        base_vals = tbl.column("base").to_pylist()
        doubled_vals = tbl.column("doubled").to_pylist()
        tripled_vals = tbl.column("tripled").to_pylist()
        assert all(d == b * 2 for b, d in zip(base_vals, doubled_vals, strict=True))
        assert all(t == b * 3 for b, t in zip(base_vals, tripled_vals, strict=True))
