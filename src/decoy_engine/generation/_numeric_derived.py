"""Whether a ``derived`` expression can read a column as a number.

Numeric typing changes what a derived expression sees: ``id * 2`` stops repeating a string, but
``id + "-x"``, ``id == "1"`` and ``id in ["1", "2"]`` were written against the cell text and give a
different answer (or an error) on an int. The check types each sub-expression as num, str, bool
or unknown, assuming the column under test is a number, and rejects the column when it meets
an operand pairing that needs text. Anything it cannot classify is rejected, so the default stays
text.
"""

from __future__ import annotations

from collections.abc import Callable

import lark

from decoy_engine.expressions import compile_expr

_ORDERING = {"==", "!=", "<", "<=", ">", ">="}
_PASS_THROUGH = {
    "start",
    "expr",
    "ternary",
    "or_expr",
    "and_expr",
    "compare_expr",
    "add_expr",
    "mul_expr",
    "not_expr",
    "unary_expr",
}
_NUM, _STR, _BOOL, _UNK = "num", "str", "bool", "unk"


def _trees(node: lark.Tree) -> list[lark.Tree]:
    return [c for c in node.children if isinstance(c, lark.Tree)]


def _core(node: lark.Tree) -> lark.Tree:
    """The node under any single-child pass-through wrappers."""
    while (
        node.data in _PASS_THROUGH
        and len(node.children) == 1
        and isinstance(node.children[0], lark.Tree)
    ):
        node = node.children[0]
    return node


def _ops(node: lark.Tree) -> list[str]:
    return [str(c) for c in node.children if isinstance(c, lark.Token)]


def derived_uses_as_numbers(
    expression: str, column: str, is_numeric: Callable[[str], bool]
) -> bool:
    """True when every use of *column* in *expression* is valid with *column* as a number.

    ``is_numeric(name)`` says whether another referenced column is currently planned numeric.
    """

    def has(node: lark.Tree) -> bool:
        return any(
            t.data == "col_ref" and str(t.children[0]) == column for t in node.iter_subtrees()
        )

    def typ(n: lark.Tree) -> str:
        d, kids = n.data, _trees(n)
        if d in _PASS_THROUGH and len(n.children) == 1:
            return typ(kids[0])
        if d == "number_lit":
            return _NUM
        if d == "string_lit" or d in ("concat_op", "slice_op"):
            return _STR
        if d in ("true_lit", "false_lit", "compare_expr", "or_expr", "and_expr", "not_op"):
            return _BOOL
        if d == "col_ref":
            name = str(n.children[0])
            return _NUM if name == column or is_numeric(name) else _UNK
        if d == "days_between_op":
            return _NUM
        if d == "paren_expr":
            return typ(kids[0])
        if d in ("neg_op", "mul_expr"):
            return _NUM if all(typ(k) == _NUM for k in kids) else _UNK
        if d == "add_expr":
            kinds = {typ(k) for k in kids}
            return kinds.pop() if len(kinds) == 1 and kinds <= {_NUM, _STR} else _UNK
        if d == "ternary":
            kinds = {typ(kids[0]), typ(kids[2])}
            return kinds.pop() if len(kinds) == 1 and kinds <= {_NUM, _STR} else _UNK
        if d == "case_when_op":
            kinds = {typ(k) for k in [*kids[1:-1:2], kids[-1]]}
            return kinds.pop() if len(kinds) == 1 and kinds <= {_NUM, _STR} else _UNK
        return _UNK

    def ok(n: lark.Tree) -> bool:
        if not has(n):
            return True
        d, kids = n.data, _trees(n)
        if d == "compare_expr":
            return _compare_ok(n)
        if d == "list_literal" or d == "empty_list":
            return False
        if not all(ok(k) for k in kids):
            return False
        if (d in _PASS_THROUGH and len(n.children) == 1) or d in (
            "paren_expr",
            "col_ref",
            "concat_op",
        ):
            return True
        if d in ("add_expr", "mul_expr", "neg_op"):
            return all(typ(k) == _NUM for k in kids)
        if d in ("or_expr", "and_expr", "not_op"):
            return all(typ(k) == _BOOL for k in kids)
        if d == "ternary":
            return len(kids) == 3 and typ(kids[1]) == _BOOL and typ(kids[0]) == typ(kids[2]) != _UNK
        if d == "case_when_op":
            values = {typ(k) for k in [*kids[1:-1:2], kids[-1]]}
            return (
                all(typ(k) == _BOOL for k in kids[:-1:2])
                and len(values) == 1
                and _UNK not in values
            )
        if d == "slice_op":
            return all(typ(k) == _NUM for k in kids[1:] if has(k))
        return False  # days_between, an unknown node kind

    def _compare_ok(n: lark.Tree) -> bool:
        kids, ops = _trees(n), _ops(n)
        for k in kids:
            if _core(k).data == "list_literal":
                if not all(ok(item) for item in _trees(_core(k))):
                    return False
            elif not ok(k):
                return False
        for i, op in enumerate(ops):
            left, right = kids[i], kids[i + 1]
            if op in _ORDERING:
                if typ(left) != _NUM or typ(right) != _NUM:
                    return False
            elif op == "in":
                items = _trees(_core(right)) if _core(right).data == "list_literal" else None
                if typ(left) != _NUM or items is None or any(typ(x) != _NUM for x in items):
                    return False
            else:
                return False
        return True

    try:
        tree = compile_expr(expression).tree
    except Exception:
        return False
    return ok(tree)
