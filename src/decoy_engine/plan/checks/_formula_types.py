"""Refuse a generate formula that uses a number reference as text.

A formula reads each referenced sequence, int or float faker column as the number it is. The
spellings below either always fail on a number (concatenation with text, a text method,
indexing, ``len``, ``join``) or silently change meaning (a compare with a text literal), so the
plan refuses them with a hint instead of letting a run write nulls or wrong cells. The scanner
targets the spellings a person writes by habit; deliberate evasion (``getattr``, rebinding a name
through a comprehension) is out of scope.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping

import pyarrow as pa

__all__ = ["check_formula_text_use"]

_SCOPES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_EQUALITY = (ast.Eq, ast.NotEq)
_MEMBERSHIP = (ast.In, ast.NotIn)


def _targets(node: ast.AST) -> set[str]:
    return {
        n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }


def _is_text(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _holds_text(node: ast.AST) -> bool:
    return _is_text(node) or (
        isinstance(node, ast.List | ast.Tuple | ast.Set) and any(_is_text(e) for e in node.elts)
    )


class _Scan:
    def __init__(self, numbers: frozenset[str], texts: frozenset[str]) -> None:
        self.numbers = numbers
        self.texts = texts
        self.found: tuple[str, str] | None = None  # (reference, detail key)

    def _number(self, node: ast.AST, bound: frozenset[str]) -> str | None:
        if isinstance(node, ast.Name) and node.id in self.numbers and node.id not in bound:
            return node.id
        return None

    def _text_name(self, node: ast.AST, bound: frozenset[str]) -> bool:
        return isinstance(node, ast.Name) and node.id in self.texts and node.id not in bound

    def _hit(self, name: str | None, detail: str) -> None:
        if name is not None and self.found is None:
            self.found = (name, detail)

    def visit(self, node: ast.AST, bound: frozenset[str]) -> None:
        if self.found is not None:
            return
        if isinstance(node, ast.Lambda):
            args = node.args
            names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
            names |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
            bound = bound | names
        elif isinstance(node, _SCOPES):
            bound = bound | _targets(node)
        self._shape(node, bound)
        for child in ast.iter_child_nodes(node):
            self.visit(child, bound)

    def _shape(self, node: ast.AST, bound: frozenset[str]) -> None:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for mine, other in ((node.left, node.right), (node.right, node.left)):
                if _is_text(other):
                    self._hit(self._number(mine, bound), "other")
        elif isinstance(node, ast.Attribute | ast.Subscript):
            self._hit(self._number(node.value, bound), "other")
        elif isinstance(node, ast.Call):
            self._call(node, bound)
        elif isinstance(node, ast.Compare):
            self._compare(node, bound)

    def _call(self, node: ast.Call, bound: frozenset[str]) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "len" and len(node.args) == 1:
            self._hit(self._number(node.args[0], bound), "other")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "join"
            and _is_text(func.value)
            and len(node.args) == 1
            and isinstance(node.args[0], ast.List | ast.Tuple)
        ):
            for element in node.args[0].elts:
                self._hit(self._number(element, bound), "other")

    def _compare(self, node: ast.Compare, bound: frozenset[str]) -> None:
        left = node.left
        for op, right in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, _EQUALITY):
                for mine, other in ((left, right), (right, left)):
                    if _holds_text(other) or self._text_name(other, bound):
                        self._hit(self._number(mine, bound), "compare")
            elif isinstance(op, _MEMBERSHIP) and (
                _holds_text(right) or self._text_name(right, bound)
            ):
                self._hit(self._number(left, bound), "compare")
            left = right


def check_formula_text_use(
    table: str,
    column: str,
    tree: ast.Expression,
    declared: list[str],
    types: Mapping[tuple[str, str], pa.DataType],
) -> None:
    """Raise ``PlanCompileError`` when *tree* uses a declared number reference as text.

    A name counts as the reference only where it is a global read: a comprehension or lambda
    variable with the same spelling is not refused. ``types`` is ``reader_types`` for the config,
    so the verdict does not depend on what else reads the column.
    """
    from decoy_engine import reasons
    from decoy_engine.plan.checks._generate_columns import _refusal

    def kind(name: str) -> pa.DataType | None:
        return types.get((table, name))

    numbers = frozenset(
        r
        for r in declared
        if (t := kind(r)) is not None and (pa.types.is_integer(t) or pa.types.is_floating(t))
    )
    if not numbers:
        return
    texts = frozenset(r for r in declared if (t := kind(r)) is not None and pa.types.is_string(t))
    scan = _Scan(numbers, texts)
    scan.visit(tree, frozenset())
    if scan.found is not None:
        reference, detail = scan.found
        code = reasons.plan.GENERATE_FORMULA_NUMBER_USED_AS_TEXT
        raise _refusal(code, table, column, "formula", ref=reference, detail=detail)
