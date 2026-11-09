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
_TOKENS = frozenset({"null_count", "non_null_count"})


def _tokens(node: ast.AST) -> set[str]:
    """Snapshot keys the node reads. Only string keys count: a snapshot column is a dict,
    while profiler code that holds `null_count` as a plain local is not reading one."""
    return {
        str(sub.value)
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and sub.value in _TOKENS
    }


def _functions(tree: ast.AST) -> list[ast.AST]:
    funcs: list[ast.AST] = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    return funcs or [tree]


def violations(relative: str, source: str) -> list[str]:
    """Rate arithmetic outside the owner: a sum over both counts, or a division in a
    function that reads both counts."""
    tree = ast.parse(source)
    out: list[str] = []
    for func in _functions(tree):
        name = getattr(func, "name", "<module>")
        if (relative, name) == OWNER:
            continue
        both = _tokens(func) == _TOKENS
        for node in ast.walk(func):
            if not isinstance(node, ast.BinOp):
                continue
            if isinstance(node.op, ast.Add) and _tokens(node) == _TOKENS:
                out.append(f"{relative}:{node.lineno} {name}: sum of both null counts")
            elif isinstance(node.op, ast.Div | ast.FloorDiv) and both:
                out.append(f"{relative}:{node.lineno} {name}: division beside both null counts")
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
