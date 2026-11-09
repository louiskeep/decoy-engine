"""AT-9 (GEN-UNIQUE): `pool_opted_out` is the only reader of a column's `pooled` key.

Five admission sites used to read ``col.get("pooled") is False`` inline. A sixth reader of
`unique` would drift from them, so the read lives in one predicate and this scan fails when a
second one appears, however it is spelled: ``col.get("pooled")``, ``col["pooled"]``,
``col.pop("pooled")``, ``getattr(col, "pooled")``, ``"pooled" in col``, or the same through an
alias. The scan is over engine ``src/`` and normalizes string constants, so a concatenated key
(``"poo" "led"``) is one constant to the AST.

Writes are not reads: a dict literal key, a ``frozenset`` entry and the declared pydantic field
are not flagged. The declared field is read as ``self.pooled`` inside the column model's own
validator; attribute access is out of scope because only dict-shaped columns drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
KEY = "pooled"
READER = ("generation/_faker_pool.py", "pool_opted_out")
READ_METHODS = frozenset({"get", "pop", "setdefault"})


def _is_key(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == KEY


def _enclosing_function(tree: ast.AST) -> dict[int, str]:
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(fn):
                owner[id(child)] = fn.name
    return owner


def pooled_reads(source: str) -> list[tuple[int, str]]:
    """(line, enclosing function or '') of every dict-style read of the `pooled` key."""
    tree = ast.parse(source)
    owner = _enclosing_function(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        hit = False
        if isinstance(node, ast.Subscript) and _is_key(node.slice):
            hit = isinstance(node.ctx, ast.Load)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in READ_METHODS:
                # Bound `col.get("pooled")` or unbound `dict.get(col, "pooled")`.
                hit = bool(node.args) and _is_key(node.args[0])
                if not hit and isinstance(func.value, ast.Name) and func.value.id == "dict":
                    hit = len(node.args) >= 2 and _is_key(node.args[1])
            elif isinstance(func, ast.Name) and func.id == "getattr":
                hit = len(node.args) >= 2 and _is_key(node.args[1])
        elif isinstance(node, ast.Compare):
            hit = any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) and _is_key(node.left)
        if hit:
            found.append((getattr(node, "lineno", 0), owner.get(id(node), "")))
    return found


def _violations() -> list[str]:
    bad: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for line, fn in pooled_reads(path.read_text(encoding="utf-8")):
            if (rel, fn) != READER:
                bad.append(f"{rel}:{line} reads the pooled key in {fn or '<module>'}")
    return bad


def test_no_pooled_read_outside_the_predicate():
    assert _violations() == []


def test_the_predicate_itself_is_found_so_the_scan_cannot_go_empty():
    text = (SRC / READER[0]).read_text(encoding="utf-8")
    assert [fn for _, fn in pooled_reads(text)] == [READER[1]]


@pytest.mark.parametrize(
    "snippet",
    [
        'x = col.get("pooled") is False',
        'x = col.get("pooled", None)',
        'x = col["pooled"]',
        'x = c.pop("pooled")',
        'x = c.setdefault("pooled", False)',
        'x = getattr(col, "pooled")',
        'x = "pooled" in col',
        'x = "pooled" not in col',
        'x = col.get("poo" "led")',
        'x = dict.get(col, "pooled")',
        "def f(col):\n    return col.get('pooled')",
    ],
)
def test_scanner_catches_each_spelling(snippet):
    # M4: one inline read left in place must be seen.
    assert pooled_reads(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        'x = {"pooled": 1}',
        'x = frozenset({"pooled"})',
        'x = col.get("pooled_other")',
        'col["pooled"] = True',
        "x = self.pooled",
    ],
)
def test_scanner_ignores_writes_and_lookalikes(snippet):
    assert not pooled_reads(snippet)
