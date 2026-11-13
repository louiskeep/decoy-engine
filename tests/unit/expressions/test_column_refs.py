"""One answer to "which columns does this expression read", from every caller.

`expressions.column_refs` is the one extractor. The generate-input resolver (an ordering edge
and a numeric-typing reader per name) and the derived-column plan check (existence and cycles)
both ask it, so a reference shape one of them missed would pass the existence check and get no
ordering edge. Each cell below pins the same set from the extractor and from both callers over
one reference shape of the grammar.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.expressions import compile_expr
from decoy_engine.plan import PlanCompileError

# (expression, the columns it reads): every grammar form that can hold a column reference.
SHAPES: list[tuple[str, frozenset[str]]] = [
    ("a", frozenset({"a"})),
    ("(a)", frozenset({"a"})),
    ("-a", frozenset({"a"})),
    ("a + b * c - e // f / g", frozenset({"a", "b", "c", "e", "f", "g"})),
    ("a >= b", frozenset({"a", "b"})),
    ('a in [b, "x", 3]', frozenset({"a", "b"})),
    ("a in []", frozenset({"a"})),
    ("a and not b or c", frozenset({"a", "b", "c"})),
    ('concat(a, "-", b)', frozenset({"a", "b"})),
    ("slice(s, start, end)", frozenset({"s", "start", "end"})),
    ("slice(s, -4)", frozenset({"s"})),
    ("days_between(a, b)", frozenset({"a", "b"})),
    ("a if b else c", frozenset({"a", "b", "c"})),
    ("if(a, b, c)", frozenset({"a", "b", "c"})),
    ("case_when(a > 1, b, c == 2, e, f)", frozenset({"a", "b", "c", "e", "f"})),
    ("if(a, concat(b, slice(c, 0, 1)), case_when(e, f, g))", frozenset("abcefg")),
    ("a + a * a", frozenset({"a"})),
    ("1 + 2", frozenset()),
    ('"a" + "b"', frozenset()),
    ("True and None == False", frozenset()),
    ("_x1 + x_2", frozenset({"_x1", "x_2"})),
]
IDS = [expr for expr, _ in SHAPES]


def _table(reads: frozenset[str], expression: str) -> dict[str, Any]:
    columns = [{"name": name, "type": "sequence", "start": 1} for name in sorted(reads)]
    derived = {"name": "out", "type": "derived", "expression": expression}
    return {"name": "t", "row_count": 3, "generate_columns": [*columns, derived]}


def _plan_check_accepts(table: dict[str, Any]) -> bool:
    from decoy_engine.plan._checks import check_derived_column_refs

    try:
        check_derived_column_refs({"tables": [table]})
    except PlanCompileError as exc:
        assert exc.code == reasons.plan.DERIVED_MISSING_COLUMN_REF
        return False
    return True


@pytest.mark.parametrize(("expression", "reads"), SHAPES, ids=IDS)
def test_the_extractor_names_every_column_the_expression_reads(
    expression: str, reads: frozenset[str]
) -> None:
    from decoy_engine.expressions import column_refs

    found = column_refs(compile_expr(expression))
    assert isinstance(found, frozenset)
    assert found == reads


@pytest.mark.parametrize(("expression", "reads"), SHAPES, ids=IDS)
def test_the_input_resolver_reads_the_same_columns(expression: str, reads: frozenset[str]) -> None:
    from decoy_engine.config import iter_generate_input_refs

    table = _table(reads, expression)
    edges = iter_generate_input_refs(table, table["generate_columns"][-1])
    assert {e.source_column for e in edges if e.option == "expression"} == reads
    # One edge per column, in a fixed order whatever the hash seed.
    names = [e.source_column for e in edges if e.option == "expression"]
    assert names == sorted(reads)


@pytest.mark.parametrize(("expression", "reads"), SHAPES, ids=IDS)
def test_the_plan_check_reads_the_same_columns(expression: str, reads: frozenset[str]) -> None:
    """Accepted with exactly `reads` declared, refused when any one of them is missing."""
    table = _table(reads, expression)
    assert _plan_check_accepts(table)
    for name in sorted(reads):
        short = {
            **table,
            "generate_columns": [c for c in table["generate_columns"] if c["name"] != name],
        }
        assert not _plan_check_accepts(short), name


def test_the_extractor_reads_a_parse_subtree_too() -> None:
    """Numeric typing asks about one operand of an expression, not the whole tree."""
    from decoy_engine.expressions import column_refs

    tree = compile_expr("concat(a, b) + c").tree
    operands = [t for t in tree.iter_subtrees() if t.data == "concat_op"]
    assert [column_refs(t) for t in operands] == [frozenset({"a", "b"})]


def test_the_extractor_is_public() -> None:
    import decoy_engine.expressions as expressions

    assert "column_refs" in expressions.__all__
