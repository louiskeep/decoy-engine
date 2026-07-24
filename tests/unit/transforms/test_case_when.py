"""Unit tests for case_when grammar extension (SP-10b TDD).

Written BEFORE the implementation per the TDD contract (testing.md).

case_when extends the closed Lark grammar so that a derived-column
expression can branch on conditions:

    case_when(cond1, val1, cond2, val2, ..., condN, valN, default)

Rules:
  - At least 3 arguments: one (cond, val) pair plus a default.
  - Conditions and values are themselves closed-grammar sub-expressions.
  - First matching condition wins; the last argument is the default.
  - The grammar stays CLOSED: only the permitted operator set parses.
    Attribute access, dunders, import, and arbitrary calls are rejected.

Methodology:
  SQL CASE WHEN conditional expression (ANSI SQL ISO/IEC 9075-2).
  Semantics: evaluate conditions left-to-right; first truthy condition
  returns its associated value; fall through to the default expression.
"""

from __future__ import annotations

import pytest

from decoy_engine.errors import ValidationError


def _compile(expr: str):
    from decoy_engine.expressions import compile_expr

    return compile_expr(expr)


def _eval(expr: str, ctx: dict) -> object:
    from decoy_engine.expressions import compile_expr, evaluate

    return evaluate(compile_expr(expr), ctx)


class TestCaseWhenParse:
    """Grammar accepts valid case_when forms and rejects invalid ones."""

    def test_single_branch_accepted(self) -> None:
        """Minimal: one cond/val pair plus a default."""
        _compile("case_when(a > 0, 1, 0)")

    def test_two_branches_accepted(self) -> None:
        _compile('case_when(a < 13, "child", a < 18, "teen", "adult")')

    def test_three_branches_accepted(self) -> None:
        _compile('case_when(score >= 90, "A", score >= 80, "B", score >= 70, "C", "D")')

    def test_value_is_closed_grammar_expression(self) -> None:
        """Values are themselves closed-grammar sub-expressions: the
        arithmetic in the value position must actually be evaluated,
        not just accepted as an opaque token."""
        expr = "case_when(a > 0, a * 2, 0)"
        assert _eval(expr, {"a": 5}) == 10
        assert _eval(expr, {"a": -1}) == 0

    def test_condition_is_closed_grammar_expression(self) -> None:
        """Conditions may reference columns and use comparison operators;
        the compound boolean condition must actually be evaluated."""
        expr = "case_when(a > 0 and b != 0, 1, 0)"
        assert _eval(expr, {"a": 5, "b": 3}) == 1
        assert _eval(expr, {"a": 5, "b": 0}) == 0

    def test_nested_case_when_in_value(self) -> None:
        """A case_when can nest another case_when in a value position,
        and the nested branch must resolve to the correct value."""
        expr = "case_when(a > 0, case_when(b > 0, 2, 1), 0)"
        assert _eval(expr, {"a": 1, "b": 1}) == 2
        assert _eval(expr, {"a": 1, "b": -1}) == 1
        assert _eval(expr, {"a": -1, "b": 1}) == 0

    def test_concat_in_value(self) -> None:
        """Closed-grammar functions are allowed in value positions,
        and concat() must actually produce the concatenated string."""
        expr = 'case_when(a > 0, concat(name, "_ok"), concat(name, "_fail"))'
        assert _eval(expr, {"a": 1, "name": "x"}) == "x_ok"
        assert _eval(expr, {"a": -1, "name": "x"}) == "x_fail"


class TestCaseWhenEval:
    """case_when evaluates correctly row by row."""

    def test_first_condition_true_returns_first_value(self) -> None:
        result = _eval('case_when(a < 13, "child", a < 18, "teen", "adult")', {"a": 10})
        assert result == "child"

    def test_second_condition_true_returns_second_value(self) -> None:
        result = _eval('case_when(a < 13, "child", a < 18, "teen", "adult")', {"a": 15})
        assert result == "teen"

    def test_no_condition_true_returns_default(self) -> None:
        result = _eval('case_when(a < 13, "child", a < 18, "teen", "adult")', {"a": 30})
        assert result == "adult"

    def test_first_match_wins(self) -> None:
        """When multiple conditions are true, the first wins."""
        result = _eval("case_when(a >= 0, 1, a >= 10, 2, 0)", {"a": 100})
        assert result == 1

    def test_default_expression_evaluated(self) -> None:
        """The default can be an expression, not just a literal."""
        result = _eval("case_when(a > 100, 99, a * 2)", {"a": 5})
        assert result == 10

    def test_numeric_values(self) -> None:
        result = _eval("case_when(tier == 1, 10, tier == 2, 20, 0)", {"tier": 2})
        assert result == 20

    def test_boolean_condition_literal(self) -> None:
        result = _eval("case_when(True, 42, 0)", {})
        assert result == 42

    def test_false_condition_falls_through(self) -> None:
        result = _eval("case_when(False, 42, 99)", {})
        assert result == 99

    def test_deterministic_same_input_same_output(self) -> None:
        """Same context produces the same result on repeated calls."""
        expr = 'case_when(a >= 18, "adult", "minor")'
        ctx = {"a": 25}
        r1 = _eval(expr, ctx)
        r2 = _eval(expr, ctx)
        assert r1 == r2

    def test_case_when_in_derived_expression(self) -> None:
        """case_when integrates with the derived strategy via apply_derived."""
        from decoy_engine.transforms.derived import DerivedConfig, apply_derived

        cfg = DerivedConfig.from_dict({"expression": 'case_when(age >= 18, "adult", "minor")'})
        assert apply_derived(cfg, {"age": 25}) == "adult"
        assert apply_derived(cfg, {"age": 10}) == "minor"


class TestCaseWhenInjectionProbes:
    """SECURITY: the extended grammar stays CLOSED.

    None of the following malicious payloads may parse or evaluate.
    Each should raise ValidationError at compile time (before row data
    is touched) because the grammar does not allow them.

    NOTE: the strings below (os.system, eval, exec, import, lambda)
    are raw string LITERALS passed to the Lark parser to verify it
    REJECTS them. They are never executed as Python. This is the
    closed-grammar security probe required by the SP-10b spec.
    """

    def _assert_rejected(self, expr: str) -> None:
        with pytest.raises(ValidationError):
            _compile(expr)

    def test_attribute_access_in_condition_rejected(self) -> None:
        """os.system() is not in the closed grammar."""
        self._assert_rejected('case_when(os.system("bad"), 1, 0)')

    def test_dunder_in_value_rejected(self) -> None:
        """__import__ is not an allowed identifier (dunder detection)."""
        self._assert_rejected('case_when(True, __import__("os"), 0)')

    def test_arbitrary_function_call_in_value_rejected(self) -> None:
        """eval() is not in the closed vocabulary."""
        self._assert_rejected('case_when(True, eval("bad"), 0)')

    def test_exec_call_rejected(self) -> None:
        self._assert_rejected('case_when(True, exec("bad"), 0)')

    def test_subscript_syntax_rejected(self) -> None:
        """dict subscript a["key"] is not in the grammar."""
        self._assert_rejected('case_when(a["key"] > 0, 1, 0)')

    def test_lambda_rejected(self) -> None:
        self._assert_rejected("case_when(True, lambda x: x, 0)")

    def test_format_string_rejected(self) -> None:
        """f-strings are not in the grammar."""
        self._assert_rejected('case_when(True, f"bad {a}", 0)')

    def test_import_statement_rejected(self) -> None:
        self._assert_rejected("case_when(True, import os, 0)")

    def test_dunder_class_in_condition_rejected(self) -> None:
        """Dunder column name __class__ is rejected at compile time."""
        self._assert_rejected("case_when(__class__ > 0, 1, 0)")

    def test_chained_attribute_access_rejected(self) -> None:
        self._assert_rejected("case_when(True, a.b.c, 0)")


class TestCaseWhenThroughRealRunPath:
    """Integration: case_when through PandasExecutionAdapter run path."""

    def test_case_when_in_mask_mode_branches_correctly(self) -> None:
        """strategy: derived with a case_when expression masks rows per condition."""
        from types import SimpleNamespace

        import pyarrow as pa

        from decoy_engine.execution import PandasExecutionAdapter
        from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        reg = get_default_registry()
        graph = RelationshipGraph(edges=(), ordering=())
        ns = NamespaceRegistry(bindings=())
        seed = b"\xca\xfe" * 4

        table = pa.table(
            {
                "age": pa.array([10, 15, 25], type=pa.int64()),
                "label": pa.array(["x", "x", "x"], type=pa.string()),
            }
        )

        def _col(strategy: str, **kw):
            return ColumnSeed(
                namespace=None,
                strategy=strategy,
                provider="x_nobackend",
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=False,
                provider_config=kw.get("provider_config", ()),
                coherent_with=(),
            )

        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=seed,
                per_table=(
                    (
                        "t",
                        TableSeed(
                            per_column=(
                                ("age", _col("passthrough")),
                                (
                                    "label",
                                    _col(
                                        "derived",
                                        provider_config=(
                                            (
                                                "expression",
                                                'case_when(age < 13, "child", age < 18, "teen", "adult")',
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                            per_group=(),
                        ),
                    ),
                ),
            )
        )

        result = PandasExecutionAdapter().run_single(
            plan, table, registry=reg, relationship_graph=graph, namespace_registry=ns
        )
        out = result.output.column("label").to_pylist()
        assert out == ["child", "teen", "adult"]

    def test_case_when_in_generate_mode_branches_correctly(self) -> None:
        """case_when in a generate table branches per row values."""
        from tests.unit._dps_helpers import compile_and_generate

        cfg = {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {},
            "tables": [
                {
                    "name": "t",
                    "row_count": 3,
                    "generate_columns": [
                        {
                            "name": "score",
                            "type": "categorical",
                            "categories": [85, 55, 30],
                            "weights": [1, 0, 0],
                        },
                        {
                            "name": "grade",
                            "type": "derived",
                            "expression": ('case_when(score >= 80, "A", score >= 50, "B", "F")'),
                        },
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
        tbl = compile_and_generate(cfg)["t"]
        grades = tbl.column("grade").to_pylist()
        # score is always 85 -> grade should be "A"
        assert grades == ["A", "A", "A"]

    def test_invalid_case_when_config_raises_at_config_parse(self) -> None:
        """An invalid case_when expression fails at DerivedConfig parse time (fail-closed)."""
        from decoy_engine.errors import ValidationError
        from decoy_engine.transforms.derived import DerivedConfig

        with pytest.raises(ValidationError):
            DerivedConfig.from_dict({"expression": 'case_when(True, eval("bad"), 0)'})
