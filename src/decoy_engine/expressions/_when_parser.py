"""Closed parser for the public `ColumnConfig.when` predicate.

Pattern: Lark LALR closed grammar (`when_grammar.lark`), the same sandbox shape as
`_lark_parser` for derived/case_when. The security boundary is the grammar: it admits
only comparisons of one column and one literal, `in`/`not in` over a literal list, and
`and`/`or`/`not` with parentheses. This module never evaluates anything; the pandas
oracle still evaluates a validated predicate with `DataFrame.eval` (numexpr, empty
scopes), and the native route reuses that same function for its row mask.

Reserved names: pandas eval resolves some identifiers to something other than a
column (numexpr function names, `inf`, `index`, the keywords). Accepting them as
column references would give a mask that silently ignores the column, so the config is
refused instead. `RESERVED_NAMES` is a frozen list pinned against numexpr's and pandas'
live sets by tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union

import lark

from decoy_engine.errors import DecoyError, ValidationError

WHEN_OUTSIDE_GRAMMAR_CODE = "when_outside_closed_grammar"

_GRAMMAR_PATH = Path(__file__).parent / "when_grammar.lark"
_PARSER = lark.Lark(
    _GRAMMAR_PATH.read_text(encoding="utf-8"), parser="lalr", maybe_placeholders=False
)

_MAX_EXPR_LENGTH = 4096
_MAX_NESTING_DEPTH = 50
# pandas eval fails well above these (64 string terms: too many numexpr inputs; 164 numeric
# terms: recursion; 200 stacked `not`), so the grammar never accepts what the oracle cannot run.
_MAX_COMPARISONS = 32
_MAX_TREE_DEPTH = 50
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_INTEGER_CHARS = frozenset("-0123456789")

# numexpr.expressions.functions (48 names), pandas' DEFAULT_GLOBALS, the names pandas
# eval resolves from the frame itself, and the Python keywords. Frozen on purpose.
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        # numexpr function names
        "abs", "arccos", "arccosh", "arcsin", "arcsinh", "arctan", "arctan2", "arctanh",
        "ceil", "complex", "conj", "contains", "copy", "copysign", "cos", "cosh", "exp",
        "expm1", "floor", "fmod", "hypot", "imag", "isfinite", "isinf", "isnan", "log",
        "log10", "log1p", "log2", "max", "maximum", "min", "minimum", "nextafter",
        "ones_like", "prod", "real", "round", "sign", "signbit", "sin", "sinh", "sqrt",
        "sum", "tan", "tanh", "trunc", "where",
        # pandas eval default globals and frame-level names
        "Timestamp", "datetime", "True", "False", "list", "tuple", "inf", "Inf",
        "nan", "NaN", "NaT", "index", "columns",
        # Python keywords and soft keywords
        "None", "and", "as", "assert", "async", "await", "break", "class", "continue",
        "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if",
        "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise",
        "return", "try", "while", "with", "yield", "match", "case", "_",
    }
)  # fmt: skip

Scalar = Union[int, float, str, bool]  # noqa: UP007 -- evaluated at import on 3.10


@dataclass(frozen=True)
class Compare:
    ref: str
    op: str
    value: Scalar
    # True when the literal was written first (`'a' == s`).
    literal_first: bool = False


@dataclass(frozen=True)
class InList:
    ref: str
    values: tuple[Scalar, ...]
    negated: bool = False


@dataclass(frozen=True)
class Not:
    operand: WhenExpr


@dataclass(frozen=True)
class BoolOp:
    op: Literal["and", "or"]
    operands: tuple[WhenExpr, ...]


WhenExpr = Union[Compare, InList, Not, BoolOp]  # noqa: UP007


class _RejectError(DecoyError):
    """A construct the closed grammar refuses; carries the reason for the config error."""


def _name(token: Any) -> str:
    name = str(token)
    if name.startswith("__") or name.endswith("__"):
        raise _RejectError("dunder identifiers are not column references")
    if name in RESERVED_NAMES:
        raise _RejectError(f"{name!r} is reserved (pandas eval would not read it as a column)")
    return name


def _number(token: Any) -> int | float:
    text = str(token)
    if set(text) <= _INTEGER_CHARS:
        value = int(text)
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise _RejectError("integer literal outside the int64 range")
        return value
    number = float(text)
    if not math.isfinite(number):
        raise _RejectError("float literal is not finite")
    return number


class _Build(lark.Transformer):  # type: ignore[type-arg]
    def start(self, items: list[Any]) -> WhenExpr:
        expr: WhenExpr = items[0]
        return expr

    def or_expr(self, items: list[Any]) -> WhenExpr:
        return BoolOp("or", tuple(items))

    def and_expr(self, items: list[Any]) -> WhenExpr:
        return BoolOp("and", tuple(items))

    def not_op(self, items: list[Any]) -> WhenExpr:
        return Not(items[0])

    def col_first(self, items: list[Any]) -> WhenExpr:
        return Compare(_name(items[0]), str(items[1]), items[2])

    def lit_first(self, items: list[Any]) -> WhenExpr:
        return Compare(_name(items[2]), str(items[1]), items[0], literal_first=True)

    def in_op(self, items: list[Any]) -> WhenExpr:
        return InList(_name(items[0]), tuple(items[1]))

    def not_in_op(self, items: list[Any]) -> WhenExpr:
        return InList(_name(items[0]), tuple(items[1]), negated=True)

    def literal_list(self, items: list[Any]) -> list[Scalar]:
        return list(items)

    def number_lit(self, items: list[Any]) -> Scalar:
        return _number(items[0])

    def str_lit(self, items: list[Any]) -> Scalar:
        return str(items[0])[1:-1]

    def true_lit(self, _items: list[Any]) -> Scalar:
        return True

    def false_lit(self, _items: list[Any]) -> Scalar:
        return False


def _reject(reason: str, cause: BaseException | None = None) -> ValidationError:
    err = ValidationError(
        f"when expression is outside the closed grammar: {reason}", code=WHEN_OUTSIDE_GRAMMAR_CODE
    )
    err.__cause__ = cause
    return err


def parse_when(expr: str) -> WhenExpr:
    """Parse `expr` into a frozen AST, or raise `ValidationError(code="when_outside_closed_grammar")`.

    The message names the position and the construct, never the expression text, so a
    caller that logs the error does not log a predicate that may embed data values.
    """
    if not isinstance(expr, str):
        raise _reject("the predicate must be a string")
    text = expr.strip()
    if not text:
        raise _reject("the predicate is empty")
    if len(text) > _MAX_EXPR_LENGTH:
        raise _reject(f"the predicate is longer than {_MAX_EXPR_LENGTH} characters")
    if text.count("(") > _MAX_NESTING_DEPTH:
        raise _reject(f"more than {_MAX_NESTING_DEPTH} parentheses")
    try:
        tree = _PARSER.parse(text)
        built: WhenExpr = _Build().transform(tree)
        _check_size(built)
        return built
    except lark.exceptions.VisitError as exc:
        orig = exc.orig_exc
        if isinstance(orig, _RejectError):
            raise _reject(str(orig), exc) from exc
        raise
    except lark.exceptions.UnexpectedInput as exc:
        raise _reject(
            f"unexpected input at position {getattr(exc, 'pos_in_stream', '?')}", exc
        ) from exc
    except lark.exceptions.LarkError as exc:
        raise _reject("the predicate does not parse", exc) from exc
    except RecursionError as exc:
        raise _reject("the predicate is nested too deeply", exc) from exc


def _check_size(ast: WhenExpr) -> None:
    """Reject a predicate with more comparisons or deeper nesting than pandas eval can run."""
    comparisons = 0
    stack: list[tuple[WhenExpr, int]] = [(ast, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > _MAX_TREE_DEPTH:
            raise _reject(f"the predicate nests more than {_MAX_TREE_DEPTH} levels")
        if isinstance(node, (Compare, InList)):
            comparisons += 1
            if comparisons > _MAX_COMPARISONS:
                raise _reject(f"the predicate has more than {_MAX_COMPARISONS} comparisons")
        elif isinstance(node, Not):
            stack.append((node.operand, depth + 1))
        else:
            stack.extend((operand, depth + 1) for operand in node.operands)


def when_column_refs(ast: WhenExpr) -> tuple[str, ...]:
    """The column names a parsed predicate reads, sorted and de-duplicated."""
    found: set[str] = set()
    stack: list[WhenExpr] = [ast]
    while stack:
        node = stack.pop()
        if isinstance(node, (Compare, InList)):
            found.add(node.ref)
        elif isinstance(node, Not):
            stack.append(node.operand)
        else:
            stack.extend(node.operands)
    return tuple(sorted(found))


__all__ = [
    "RESERVED_NAMES",
    "WHEN_OUTSIDE_GRAMMAR_CODE",
    "BoolOp",
    "Compare",
    "InList",
    "Not",
    "WhenExpr",
    "parse_when",
    "when_column_refs",
]
