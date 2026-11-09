"""AT-13 (engine half): the snapshot null rate has one source, `snapshot_null_fraction`.

Fidelity, diagnostic and the CLI fit summary must read the same count and the same
zero-row rule, so a fresh division over a column's `null_count` and `non_null_count`
anywhere else in the engine fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"

OWNER = ("quality/diagnostic.py", "snapshot_null_fraction")

# Exact (path, function) sites that divide beside the counts without computing a null rate.
ALLOWED: dict[tuple[str, str], str] = {
    ("generation/statistical/_sample.py", "sample_column"): (
        "blank_count / non-null cells is the blank share, not the null rate"
    ),
}
_COUNTS = frozenset({"null_count", "non_null_count"})
_TOKENS = _COUNTS | {"row_count"}


def _tokens(node: ast.AST) -> set[str]:
    """Snapshot keys the node reads, as a string key (`col["null_count"]`) or an attribute
    (`fmt.null_count`). A bare local named `null_count` does not count: profiler code that
    holds one is not reading a snapshot column."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value in _TOKENS:
            found.add(str(sub.value))
        elif isinstance(sub, ast.Attribute) and sub.attr in _TOKENS:
            found.add(sub.attr)
    return found


def _functions(tree: ast.AST) -> list[ast.AST]:
    funcs: list[ast.AST] = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    return funcs or [tree]


def violations(relative: str, source: str) -> list[str]:
    """Rate arithmetic outside the owner: a sum over both counts, or a division in a function
    that reads `null_count` beside `non_null_count` or `row_count`."""
    tree = ast.parse(source)
    out: list[str] = []
    for func in _functions(tree):
        name = getattr(func, "name", "<module>")
        if (relative, name) == OWNER or (relative, name) in ALLOWED:
            continue
        seen = _tokens(func)
        rate_inputs = "null_count" in seen and bool(seen & {"non_null_count", "row_count"})
        for node in ast.walk(func):
            if not isinstance(node, ast.BinOp):
                continue
            if isinstance(node.op, ast.Add) and _tokens(node) >= _COUNTS:
                out.append(f"{relative}:{node.lineno} {name}: sum of both null counts")
            elif isinstance(node.op, ast.Div | ast.FloorDiv) and rate_inputs:
                out.append(f"{relative}:{node.lineno} {name}: null rate division")
    return out


def test_engine_has_one_null_fraction_source() -> None:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        found.extend(violations(rel, path.read_text(encoding="utf-8")))
    assert found == []


def test_owner_is_present_and_exempt() -> None:
    relative, name = OWNER
    source = (SRC / relative).read_text(encoding="utf-8")
    assert f"def {name}(" in source
    assert violations(relative, source) == []


def test_seeded_direct_division_in_fidelity_is_detected() -> None:
    seeded = (
        "def _missing(col):\n"
        "    total = col['null_count'] + col['non_null_count']\n"
        "    return col['null_count'] / total\n"
    )
    assert violations("quality/fidelity.py", seeded)


def test_seeded_division_through_locals_is_detected() -> None:
    seeded = (
        "def _missing(col):\n"
        "    nulls = col['null_count']\n"
        "    kept = col['non_null_count']\n"
        "    return nulls / (nulls + kept)\n"
    )
    assert violations("quality/fidelity.py", seeded)


def test_unrelated_count_arithmetic_is_not_flagged() -> None:
    fine = (
        "def released(row_count, non_null_count):\n"
        "    return {'null_count': max(0, row_count - non_null_count),\n"
        "            'non_null_count': non_null_count}\n"
    )
    assert violations("quality/dp.py", fine) == []


def test_seeded_division_by_row_count_is_detected() -> None:
    seeded = "def rate(col, snap):\n    return col['null_count'] / snap['row_count']\n"
    assert violations("quality/fidelity.py", seeded)


def test_seeded_attribute_form_is_detected() -> None:
    seeded = "def rate(c):\n    return c.null_count / (c.null_count + c.non_null_count)\n"
    assert violations("quality/fidelity.py", seeded)


def test_seeded_attribute_division_by_row_count_is_detected() -> None:
    seeded = "def rate(fmt):\n    return fmt.null_count / fmt.row_count\n"
    assert violations("generation/statistical/_sample.py", seeded)
