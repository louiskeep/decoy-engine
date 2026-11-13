"""GEN-REL: "is this column a sequence that keeps every key" has one owner.

`config.is_exact_sequence_key` (`config/_generate_relational.py`) is the rule: a `sequence` with
no positive `null_probability`, failing closed on a rate it cannot read. The self-reference key
check, the capacity probe's bounds and the plan's weights-length check each used to restate it,
and the copies drifted on rates that are not plain numbers. This sentry fails on a restatement.

Scope, honestly: a function outside the owner that reads the `null_probability` key and tests
`== "sequence"` as an operand of an `and`. A dispatcher that branches on `kind == "sequence"`
in its own `if`, or tests `!= "sequence"`, is not the rule and is not flagged. A restatement
spread across helpers, or written without `and`, is not seen. It also checks that the three
known callers call the predicate. Pure AST: nothing scanned is imported.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
PREDICATE = "is_exact_sequence_key"
OWNER = ("config/_generate_relational.py", PREDICATE)
CALLERS = frozenset(
    {
        ("config/_generate_relational.py", "parent_key_problem"),
        ("execution/_probe_scale.py", "_loosen_per_parent_bounds"),
        ("plan/checks/_generate_columns.py", "_check_reference_weights"),
    }
)
NULLS_KEY = "null_probability"


def _functions(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """(qualified name, node) of each function, methods as `Class.method`."""
    found: list[tuple[str, ast.AST]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((f"{prefix}{child.name}", child))
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def _is_sequence_test(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], ast.Eq):
        return False
    sides = (node.left, node.comparators[0])
    return any(isinstance(s, ast.Constant) and s.value == "sequence" for s in sides)


def _restates(fn: ast.AST) -> bool:
    reads_nulls = any(isinstance(n, ast.Constant) and n.value == NULLS_KEY for n in ast.walk(fn))
    tests_sequence_in_and = any(
        isinstance(n, ast.BoolOp)
        and isinstance(n.op, ast.And)
        and any(_is_sequence_test(v) for v in n.values)
        for n in ast.walk(fn)
    )
    return reads_nulls and tests_sequence_in_and


def restatements(source: str, rel: str) -> list[str]:
    """`rel:function` of each function in `source` that restates the rule."""
    return [
        f"{rel}:{name}"
        for name, fn in _functions(ast.parse(source))
        if (rel, name) != OWNER and _restates(fn)
    ]


def callers(source: str) -> set[str]:
    """Functions in `source` that call the predicate by name or attribute."""
    return {
        name
        for name, fn in _functions(ast.parse(source))
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == PREDICATE)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == PREDICATE)
        )
    }


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def test_no_module_restates_the_exact_sequence_key_rule() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 100, "the scan must not go empty"
    found = [hit for p in files for hit in restatements(p.read_text(encoding="utf-8"), _rel(p))]
    assert found == []


def test_the_owner_defines_the_rule_and_the_listed_callers_use_it() -> None:
    owner_source = (SRC / OWNER[0]).read_text(encoding="utf-8")
    assert OWNER[1] in {name for name, _ in _functions(ast.parse(owner_source))}
    for rel, function in sorted(CALLERS):
        assert function in callers((SRC / rel).read_text(encoding="utf-8")), (rel, function)


# ---- meta: the scan sees each shape the copies took, and leaves dispatchers alone -----------


@pytest.mark.parametrize(
    "body",
    [
        # one expression, read with `or 0`
        'def f(key):\n    exact = key.get("type") == "sequence" and not (key.get("null_probability") or 0) > 0\n',
        # the read split from the test across two statements
        "def f(c, w, rows):\n"
        '    keeps = not (c.get("null_probability") or 0) > 0\n'
        '    if c.get("type") == "sequence" and keeps and len(w) != rows:\n'
        "        raise ValueError\n",
        # a typed read, the constant on the left, a subscript
        'def f(c):\n    return "sequence" == c["type"] and not on(c["null_probability"])\n',
        # a method
        "class K:\n"
        "    def f(self, c):\n"
        '        return c.get("type") == "sequence" and c.get("null_probability") is None\n',
    ],
    ids=["one-expression", "split", "typed", "method"],
)
def test_seeded_restatements_are_seen(body: str) -> None:
    assert restatements(body, "generation/x.py") != []


@pytest.mark.parametrize(
    "body",
    [
        'def f(c, kind):\n    if kind == "sequence":\n        return 1\n'
        '    return float(c.get("null_probability") or 0.0)\n',
        'def f(c, kind):\n    if c.get("unique") is True and kind != "sequence":\n        return 1\n'
        '    return c.get("null_probability")\n',
        'def f(c):\n    return c.get("type") == "sequence" and is_exact_sequence_key(c)\n',
    ],
    ids=["dispatcher", "not-equal", "uses-predicate"],
)
def test_dispatchers_and_predicate_calls_are_not_restatements(body: str) -> None:
    assert restatements(body, "generation/x.py") == []


def test_the_owner_function_alone_is_exempt() -> None:
    body = 'def is_exact_sequence_key(c):\n    return c.get("type") == "sequence" and not c.get("null_probability")\n'
    assert restatements(body, OWNER[0]) == []
    assert restatements(body, "execution/_probe_scale.py") != []
    assert restatements(body.replace(PREDICATE, "_other"), OWNER[0]) != []


def test_seeded_a_caller_that_stops_calling_is_seen() -> None:
    assert callers("def g(c):\n    return config.is_exact_sequence_key(c)\n") == {"g"}
    assert callers("def g(c):\n    return is_exact_sequence_key(c)\n") == {"g"}
    assert callers('def g(c):\n    return c.get("type") == "sequence"\n') == set()
