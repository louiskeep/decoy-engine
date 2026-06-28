"""Closed-vocabulary expression parser for derived / case_when strategies.

Pattern: Lark EBNF parser generator (lark-parser/lark, MIT).
See: https://github.com/lark-parser/lark

SECURITY DESIGN: config-supplied expressions route through a CLOSED Lark
grammar (grammar.lark in this package). Only the explicit operator set
(arithmetic, comparison, logical, membership, concat, days_between,
ternary, literals, column references) can parse. Any expression outside
the grammar -- function calls other than concat/days_between, attribute
access, dunder identifiers, subscript syntax, import -- produces a Lark
ParseError converted to ValidationError at compile time (before any row
data is touched). There is NO Python eval(), exec(), or __import__ here.
The security boundary IS the grammar.

API:
  compile_expr(expr_string) -> CompiledExpression
    One-time parse per column expression. Raises ValidationError on
    any expression outside the closed grammar.

  evaluate(compiled, row_context) -> value
    Per-row evaluation. row_context is {column_name: value}. Raises
    KeyError when a referenced column is absent from the context.

Date arithmetic follows ISO 8601: strings are parsed with
datetime.date.fromisoformat.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lark

from decoy_engine.errors import ValidationError

_GRAMMAR_PATH = Path(__file__).parent / "grammar.lark"

# Module-level singleton: grammar compiled once at import time.
# earley handles the slightly ambiguous ternary rule cleanly; lalr(1)
# is faster but requires a fully unambiguous grammar. earley is fine
# for the 10k-eval/s budget; measured at <0.05ms/parse.
_PARSER = lark.Lark(
    _GRAMMAR_PATH.read_text(encoding="utf-8"),
    parser="earley",
    ambiguity="resolve",
)

# Safety bounds applied BEFORE parsing to prevent RecursionError and
# to keep compile times predictable. Any expression outside these bounds
# is rejected with a clear ValidationError.
_MAX_EXPR_LENGTH = 4096  # ~4 KB -- far beyond any realistic column expression
_MAX_NESTING_DEPTH = 50  # open-paren depth proxy


@dataclass(frozen=True)
class CompiledExpression:
    """A parsed, validated expression ready for per-row evaluation.

    Instances are immutable and safe to share across threads and rows.

    Attributes:
        source: The original expression string (for error messages).
        tree: The Lark parse tree produced at compile time.
    """

    source: str
    tree: lark.Tree[Any]


def compile_expr(expr_string: str) -> CompiledExpression:
    """Parse and validate ``expr_string`` against the closed grammar.

    Called once per column at pipeline-compile time. Raises
    :class:`~decoy_engine.errors.ValidationError` if the expression uses
    any operator, construct, or identifier outside the permitted set.

    Safety bounds (applied before parsing):
      - Maximum expression length: 4096 characters.
      - Maximum parenthesis nesting depth: 50 levels.
    These prevent RecursionError and keep compile times predictable.

    String literal escapes (e.g. ``\\n``, ``\\t``, ``\\uXXXX``) are
    validated at compile time so a bad escape fails immediately rather
    than crashing per-row at evaluate time.

    Note: string literals must use double quotes. Single-quoted strings
    (e.g. ``'hello'``) are not in the closed grammar; use ``"hello"``.

    Args:
        expr_string: A column-level expression string, e.g. ``"a + b"``.

    Returns:
        A :class:`CompiledExpression` ready for :func:`evaluate`.

    Raises:
        ValidationError: Expression is outside the closed operator set,
            exceeds the safety bounds, or contains an invalid string
            escape sequence.
    """
    expr_string = expr_string.strip()

    # M1: length bound -- reject before touching the parser.
    if len(expr_string) > _MAX_EXPR_LENGTH:
        raise ValidationError(
            f"expression is too long ({len(expr_string)} chars); "
            f"maximum allowed length is {_MAX_EXPR_LENGTH} chars. "
            f"Simplify the expression or split it across multiple columns."
        )

    # M1: nesting-depth bound -- count open-paren depth as a proxy.
    depth = 0
    max_depth = 0
    for ch in expr_string:
        if ch == "(":
            depth += 1
            if depth > max_depth:
                max_depth = depth
        elif ch == ")":
            depth -= 1
    if max_depth > _MAX_NESTING_DEPTH:
        raise ValidationError(
            f"expression nesting depth {max_depth} exceeds the maximum "
            f"{_MAX_NESTING_DEPTH}. Refactor deeply nested subexpressions "
            f"into intermediate columns."
        )

    try:
        tree = _PARSER.parse(expr_string)
    except (
        lark.exceptions.ParseError,
        lark.exceptions.UnexpectedCharacters,
        lark.exceptions.UnexpectedToken,
        lark.exceptions.UnexpectedEOF,
    ) as exc:
        # L1: if the expression contains single quotes, add a targeted hint.
        single_quote_hint = (
            " Note: string literals must use double quotes, not single quotes "
            "(e.g. \"hello\" not 'hello')."
            if "'" in expr_string
            else ""
        )
        raise ValidationError(
            f"unsupported expression: {expr_string!r} is outside the closed "
            f"operator set. Only arithmetic (+,-,*,/,//), comparison "
            f"(==,!=,<,>,<=,>=,in), logical (and,or,not), concat(), "
            f"days_between(), if/else ternary, literals, and column "
            f"references are allowed. Workaround: use a formula strategy "
            f"for arbitrary Python expressions.{single_quote_hint} Detail: {exc}"
        ) from exc
    except RecursionError as exc:
        # Belt-and-suspenders: the depth bound above prevents most cases,
        # but catch any RecursionError that slips through and surface it
        # as ValidationError so the closed-grammar contract holds.
        raise ValidationError(
            f"expression is too deeply nested to parse (exceeded Python "
            f"recursion limit). Maximum nesting depth is {_MAX_NESTING_DEPTH}. "
            f"Refactor deeply nested subexpressions into intermediate columns."
        ) from exc

    # Walk the parse tree and reject dunder identifiers. The grammar's
    # IDENTIFIER regex allows them (underscore is a valid leading char),
    # but dunder names (__class__, __builtins__, etc.) are never valid
    # column references. Checked here (compile time) not evaluate time.
    for subtree in tree.iter_subtrees():
        if subtree.data == "col_ref":
            name = str(subtree.children[0])
            if name.startswith("__"):
                raise ValidationError(
                    f"unsupported expression: dunder identifier {name!r} is not "
                    f"allowed. Column references must be plain identifiers."
                )

    # M2: validate all string literal escape sequences at compile time
    # so a malformed escape fails immediately rather than crashing
    # per-row inside evaluate().
    _validate_string_escapes(tree, expr_string)

    return CompiledExpression(source=expr_string, tree=tree)


def _validate_string_escapes(tree: lark.Tree[Any], source: str) -> None:
    """Validate escape sequences in all string literals in *tree*.

    Walks the parse tree and attempts to decode each string token using
    the same codec used at evaluate time. Raises ValidationError for any
    token whose escape sequence cannot be decoded so the error is surfaced
    at compile time rather than crashing per-row inside evaluate().
    """
    for subtree in tree.iter_subtrees():
        if subtree.data == "string_lit":
            raw = str(subtree.children[0])
            content = raw[1:-1]  # strip surrounding double-quote chars
            try:
                content.encode("raw_unicode_escape").decode("unicode_escape")
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValidationError(
                    f"invalid string escape sequence in {raw!r} "
                    f"(in expression {source!r}): {exc}. "
                    f"Check that all backslash sequences are valid "
                    f"(e.g. \\\\n, \\\\t, \\\\\\\\, \\\\uXXXX). "
                    f"Raw Windows paths such as C:\\\\xyz should escape "
                    f"the backslash (C:\\\\\\\\xyz) or use forward slashes."
                ) from exc


def evaluate(compiled: CompiledExpression, row_context: dict[str, Any]) -> Any:
    """Evaluate a pre-compiled expression against one row's values.

    Args:
        compiled: Result of :func:`compile_expr`.
        row_context: Mapping of column name to its value for this row.

    Returns:
        The expression result (numeric, string, bool, None, or list).

    Raises:
        KeyError: A column reference is not in ``row_context``.
        ValidationError: A dunder identifier was referenced.
    """
    transformer = _ExprTransformer(row_context)
    try:
        return transformer.transform(compiled.tree)
    except lark.exceptions.VisitError as exc:
        # Lark wraps transformer exceptions in VisitError; unwrap so
        # callers see KeyError / ValidationError directly.
        ctx = exc.__context__
        if ctx is not None:
            raise ctx from ctx
        raise


class _ExprTransformer(lark.Transformer):  # type: ignore[type-arg]
    """Transforms a Lark parse tree into a Python value.

    Each method corresponds to a rule alias in grammar.lark. Lark calls
    them bottom-up: leaves are evaluated first, parent rules receive
    their children already resolved.
    """

    def __init__(self, row_context: dict[str, Any]) -> None:
        super().__init__()
        self._ctx = row_context

    # ---- structural pass-throughs ----------------------------------------

    def start(self, items: list[Any]) -> Any:
        return items[0]

    def expr(self, items: list[Any]) -> Any:
        return items[0]

    # ---- ternary  (value if condition else other) -------------------------

    def ternary(self, items: list[Any]) -> Any:
        if len(items) == 1:
            return items[0]
        # Grammar: or_expr "if" or_expr "else" ternary
        # items: [value_expr, condition_expr, else_expr]
        value, condition, else_expr = items
        return value if condition else else_expr

    # ---- logical operators -----------------------------------------------

    def or_expr(self, items: list[Any]) -> Any:
        result = items[0]
        for operand in items[1:]:
            result = result or operand
        return result

    def and_expr(self, items: list[Any]) -> Any:
        result = items[0]
        for operand in items[1:]:
            result = result and operand
        return result

    def not_op(self, items: list[Any]) -> Any:
        return not items[0]

    def not_expr(self, items: list[Any]) -> Any:
        # Pass-through when the "not" keyword is absent (-> compare_expr branch).
        return items[0]

    def unary_expr(self, items: list[Any]) -> Any:
        # Pass-through when negation is absent (-> primary branch).
        return items[0]

    # ---- comparison operators --------------------------------------------

    def compare_expr(self, items: list[Any]) -> Any:
        # items: [left] when no operator, or [left, CMP_OP_token, right, ...]
        if len(items) == 1:
            return items[0]
        left = items[0]
        i = 1
        result: Any = True
        while i < len(items):
            op = str(items[i])
            right = items[i + 1]
            i += 2
            if op == "==":
                cmp = left == right
            elif op == "!=":
                cmp = left != right
            elif op == "<":
                cmp = left < right
            elif op == ">":
                cmp = left > right
            elif op == "<=":
                cmp = left <= right
            elif op == ">=":
                cmp = left >= right
            else:  # "in"
                cmp = left in right
            result = result and cmp
            left = right
        return result

    # ---- arithmetic operators --------------------------------------------

    def add_expr(self, items: list[Any]) -> Any:
        result = items[0]
        i = 1
        while i < len(items):
            op = str(items[i])
            right = items[i + 1]
            i += 2
            result = result + right if op == "+" else result - right
        return result

    def mul_expr(self, items: list[Any]) -> Any:
        result = items[0]
        i = 1
        while i < len(items):
            op = str(items[i])
            right = items[i + 1]
            i += 2
            if op == "*":
                result = result * right
            elif op == "/":
                result = result / right
            else:  # "//"
                result = result // right
        return result

    def neg_op(self, items: list[Any]) -> Any:
        return -items[0]

    # ---- built-in functions (closed set only) ----------------------------

    def concat_op(self, items: list[Any]) -> str:
        return str(items[0]) + str(items[1])

    def days_between_op(self, items: list[Any]) -> int:
        start = _parse_date(items[0])
        end = _parse_date(items[1])
        return (end - start).days

    # ---- primary atoms --------------------------------------------------

    def paren_expr(self, items: list[Any]) -> Any:
        return items[0]

    def empty_list(self, items: list[Any]) -> list[Any]:
        return []

    def list_literal(self, items: list[Any]) -> list[Any]:
        return list(items)

    def number_lit(self, items: list[Any]) -> float | int:
        raw = str(items[0])
        if "." in raw or "e" in raw.lower():
            return float(raw)
        return int(raw)

    def string_lit(self, items: list[Any]) -> str:
        # ESCAPED_STRING includes surrounding double-quote chars.
        raw = str(items[0])
        # Decode standard escape sequences (\n, \t, \\, etc.).
        return raw[1:-1].encode("raw_unicode_escape").decode("unicode_escape")

    def true_lit(self, items: list[Any]) -> bool:
        return True

    def false_lit(self, items: list[Any]) -> bool:
        return False

    def none_lit(self, items: list[Any]) -> None:
        return None

    def col_ref(self, items: list[Any]) -> Any:
        name = str(items[0])
        # Dunder identifiers (__class__, __builtins__, etc.) are not
        # column names. Reject them at evaluation time so a grammar
        # change cannot inadvertently reopen the dunder surface.
        if name.startswith("__"):
            raise ValidationError(
                f"unsupported expression: dunder identifier {name!r} is not "
                f"allowed as a column reference. Use a plain column name."
            )
        return self._ctx[name]


def _parse_date(value: Any) -> datetime.date:
    """Parse a date value for days_between.

    Accepts datetime.date objects directly or ISO-8601 strings
    (YYYY-MM-DD).
    """
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))
