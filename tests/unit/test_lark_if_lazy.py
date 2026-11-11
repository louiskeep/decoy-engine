"""``if(c, a, b)`` in the expression grammar and lazy conditional evaluation.

GEN-TYPES E4 (AT-12 to AT-14, AT-18). ``if(...)`` is sugar for the ternary,
rewritten at compile, and conditionals evaluate only the chosen branch.
"""

from __future__ import annotations

import random

import pytest

from decoy_engine.errors import ValidationError
from decoy_engine.expressions import compile_expr, evaluate
from decoy_engine.generation._numeric_derived import derived_uses_as_numbers
from decoy_engine.transforms.derived import DerivedConfig, _get_column_refs, apply_derived


def _ev(expr: str, ctx: dict) -> object:
    return evaluate(compile_expr(expr), ctx)


def _shape(expr: str) -> list[str]:
    return [str(t.data) for t in compile_expr(expr).tree.iter_subtrees()]


class TestIfFunctionParse:
    """AT-12."""

    def test_if_function_values(self) -> None:
        assert _ev("if(x > 0, 1, 2)", {"x": 5}) == 1
        assert _ev("if(x > 0, 1, 2)", {"x": -5}) == 2

    def test_space_before_paren(self) -> None:
        assert _ev("if (x > 0, 1, 2)", {"x": 5}) == 1

    def test_nested_ternary_in_branch(self) -> None:
        expr = "if(c, a if d else e, b)"
        assert _ev(expr, {"c": True, "d": True, "a": 1, "e": 2, "b": 3}) == 1
        assert _ev(expr, {"c": True, "d": False, "a": 1, "e": 2, "b": 3}) == 2
        assert _ev(expr, {"c": False, "d": False, "a": 1, "e": 2, "b": 3}) == 3

    def test_if_function_as_ternary_condition(self) -> None:
        expr = "x if if(c, a, b) else y"
        assert _ev(expr, {"x": "X", "y": "Y", "c": True, "a": True, "b": False}) == "X"
        assert _ev(expr, {"x": "X", "y": "Y", "c": False, "a": True, "b": False}) == "Y"

    def test_nested_if_functions(self) -> None:
        expr = "if(a > 0, if(b > 0, 1, 2), 3)"
        assert _ev(expr, {"a": 1, "b": 1}) == 1
        assert _ev(expr, {"a": 1, "b": -1}) == 2
        assert _ev(expr, {"a": -1, "b": 1}) == 3

    def test_identifier_prefixed_if_stays_a_column(self) -> None:
        assert _ev("iffy + 1", {"iffy": 4}) == 5
        assert _ev("if_a + 1", {"if_a": 4}) == 5

    @pytest.mark.parametrize("bad", ["if(c, a)", "if(c)", "if(c, a, b, d)", "if()"])
    def test_wrong_arity_refused(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            compile_expr(bad)

    def test_if_node_never_reaches_a_reader(self) -> None:
        assert "if_op" not in _shape("if(c, a, b)")
        assert "if_op" not in _shape("if(if(c, a, b), if(d, 1, 2), 3)")


class TestLazyConditionals:
    """AT-13."""

    def test_if_skips_unchosen_division(self) -> None:
        assert _ev("if(a == 0, 0, 100 // a)", {"a": 0}) == 0
        assert _ev("if(a == 0, 0, 100 // a)", {"a": 5}) == 20

    def test_ternary_skips_unchosen_division(self) -> None:
        assert _ev("0 if a == 0 else 100 // a", {"a": 0}) == 0

    def test_case_when_skips_unchosen_division(self) -> None:
        expr = "case_when(a == 0, 0, a > 0, 100 // a, 100 / a)"
        assert _ev(expr, {"a": 0}) == 0
        assert _ev(expr, {"a": 4}) == 25

    def test_case_when_skips_values_after_the_match(self) -> None:
        assert _ev("case_when(a == 0, 7, b // a > 0, 1, 2)", {"a": 0, "b": 3}) == 7

    def test_or_and_short_circuit(self) -> None:
        assert _ev("a == 0 or 10 // a > 1", {"a": 0}) is True
        assert _ev("a != 0 and 10 // a > 1", {"a": 0}) is False

    def test_chosen_branch_still_raises(self) -> None:
        with pytest.raises(ZeroDivisionError):
            _ev("if(a == 0, 100 // a, 1)", {"a": 0})

    def test_unchosen_branch_missing_column_is_not_read(self) -> None:
        assert _ev("if(c, a, missing)", {"c": True, "a": 1}) == 1

    def test_condition_error_still_raises(self) -> None:
        with pytest.raises(KeyError):
            _ev("if(missing, 1, 2)", {})

    def test_generate_and_mask_derived_share_the_evaluator(self) -> None:
        cfg = DerivedConfig.from_dict({"expression": "if(a == 0, 0, 100 // a)"})
        assert apply_derived(cfg, {"a": 0}, column="c", row_index=0) == 0
        assert apply_derived(cfg, {"a": 4}, column="c", row_index=1) == 25
        case = DerivedConfig.from_dict({"expression": "case_when(a == 0, 0, a > 0, 100 // a, 1)"})
        assert apply_derived(case, {"a": 0}, column="c", row_index=0) == 0


_CORPUS = [
    ("if(a > b, a - b, b - a)", lambda a, b: (a - b) if a > b else (b - a)),
    ("if(a > 0 and b > 0, a * b, 0)", lambda a, b: (a * b) if (a > 0 and b > 0) else 0),
    ("if(a > 0 or b > 0, a + b, 0)", lambda a, b: (a + b) if (a > 0 or b > 0) else 0),
    ("a + b if a > b else a - b", lambda a, b: (a + b) if a > b else (a - b)),
    ("if(not a > 0, 1, if(b > 0, 2, 3))", lambda a, b: 1 if not a > 0 else (2 if b > 0 else 3)),
    ("if(a == 0, 0, 100 // a)", lambda a, b: 0 if a == 0 else 100 // a),
]


class TestCorpusReplay:
    """AT-14: same value wherever the reference evaluates."""

    @pytest.mark.parametrize(("ours", "ref"), _CORPUS)
    def test_values_match_reference(self, ours: str, ref) -> None:
        rng = random.Random(14)
        compiled = compile_expr(ours)
        for _ in range(500):
            a, b = rng.randint(-5, 5), rng.randint(-5, 5)
            assert evaluate(compiled, {"a": a, "b": b}) == ref(a, b), (ours, a, b)

    def test_only_the_chosen_branch_runs(self) -> None:
        # `100 // a` is the only branch that can raise; when it is unchosen no row raises.
        compiled = compile_expr("if(a == 0, 0, 100 // a)")
        for a in range(-3, 4):
            assert evaluate(compiled, {"a": a}) == (0 if a == 0 else 100 // a)

    def test_error_class_stable_when_branch_is_chosen(self) -> None:
        for expr in ("if(a == 0, 1 // a, 2)", "1 // a if a == 0 else 2"):
            with pytest.raises(ZeroDivisionError):
                _ev(expr, {"a": 0})

    def test_existing_non_conditional_expression_unchanged(self) -> None:
        assert _ev('concat(first, " ", last)', {"first": "A", "last": "B"}) == "A B"
        assert _ev("a in [1, 2, 3]", {"a": 2}) is True
        assert _ev("1 < a < 5", {"a": 3}) is True


class TestIfTypesLikeTernary:
    """AT-18."""

    @staticmethod
    def _numbers(expr: str) -> bool:
        return derived_uses_as_numbers(expr, "out", lambda name: name in {"a", "b"})

    @pytest.mark.parametrize(
        ("fn", "tern"),
        [
            ("if(a > 0, a, b)", "a if a > 0 else b"),
            ("if(a > 0, out + 1, 0)", "out + 1 if a > 0 else 0"),
            ('if(a > 0, "x", "y")', '"x" if a > 0 else "y"'),
            ('if(a > 0, out, "y")', 'out if a > 0 else "y"'),
        ],
    )
    def test_same_numeric_typing(self, fn: str, tern: str) -> None:
        assert self._numbers(fn) == self._numbers(tern)

    def test_numeric_if_is_numeric(self) -> None:
        assert self._numbers("if(a > 0, out + 1, 0)") is True

    def test_same_column_refs(self) -> None:
        a = _get_column_refs(compile_expr("if(c, x, y)"))
        b = _get_column_refs(compile_expr("x if c else y"))
        assert a == b == frozenset({"c", "x", "y"})

    def test_missing_ref_in_either_branch_is_visible_to_the_ref_check(self) -> None:
        refs = _get_column_refs(compile_expr("if(c, x, if(d, y, z))"))
        assert refs == frozenset({"c", "x", "d", "y", "z"})
