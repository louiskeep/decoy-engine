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

_COUNTS = frozenset({"null_count", "non_null_count"})
# Other spellings of a count concept. `CellFormat.non_null_cells` is the snapshot's
# `non_null_count`. Add an alias here only: _TOKENS derives from this map.
_ALIASES = {"non_null_cells": "non_null_count"}
_TOKENS = _COUNTS | {"row_count"} | _ALIASES.keys()


def _tokens(node: ast.AST) -> set[str]:
    """Snapshot keys the node reads, as a string key (`col["null_count"]`) or an attribute
    (`fmt.null_count`). A bare local named `null_count` does not count: profiler code that
    holds one is not reading a snapshot column."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value in _TOKENS:
            found.add(_ALIASES.get(str(sub.value), str(sub.value)))
        elif isinstance(sub, ast.Attribute) and sub.attr in _TOKENS:
            found.add(_ALIASES.get(sub.attr, sub.attr))
    return found


def _tokens_here(node: ast.AST) -> set[str]:
    """Tokens a single node carries itself (the scope walk already visits every node)."""
    if isinstance(node, ast.Constant) and node.value in _TOKENS:
        return {_ALIASES.get(str(node.value), str(node.value))}
    if isinstance(node, ast.Attribute) and node.attr in _TOKENS:
        return {_ALIASES.get(node.attr, node.attr)}
    return set()


def _own_nodes(root: ast.AST) -> list[ast.AST]:
    """Nodes of one scope: a function body, or the module/class level without the functions."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(root))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _scopes(tree: ast.Module) -> list[tuple[str, list[ast.AST]]]:
    """Every function (nested ones too) and the module scope, each with only its own nodes,
    so module-level arithmetic is scanned even when the file also defines functions."""
    scopes: list[tuple[str, list[ast.AST]]] = [("<module>", _own_nodes(tree))]
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
            scopes.append((n.name, _own_nodes(n)))
    return scopes


def violations(relative: str, source: str) -> list[str]:
    """Rate arithmetic outside the owner: a sum over both counts, or a division in a scope
    that reads `null_count` beside `non_null_count` or `row_count`."""
    out: list[str] = []
    for name, nodes in _scopes(ast.parse(source)):
        if (relative, name) == OWNER:
            continue
        seen: set[str] = set()
        for node in nodes:
            seen |= _tokens_here(node)
        rate_inputs = "null_count" in seen and bool(seen & {"non_null_count", "row_count"})
        for node in nodes:
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


def test_seeded_division_inside_sample_column_is_detected() -> None:
    seeded = (
        "def sample_column(fmt):\n"
        "    blank = fmt.blank_count / fmt.non_null_cells\n"
        "    return fmt.null_count / fmt.row_count\n"
    )
    assert violations("generation/statistical/_sample.py", seeded)


def test_seeded_module_level_division_is_detected() -> None:
    seeded = "def other():\n    return 1\n\nRATE = FMT.null_count / FMT.row_count\n"
    assert violations("generation/statistical/_sample.py", seeded)


def test_seeded_class_level_division_is_detected() -> None:
    seeded = "class K:\n    RATE = FMT.null_count / FMT.row_count\n"
    assert violations("generation/statistical/_sample.py", seeded)


def test_blank_share_without_null_tokens_is_not_flagged() -> None:
    fine = "def sample_column(fmt):\n    return fmt.blank_count / fmt.non_null_cells\n"
    assert violations("generation/statistical/_sample.py", fine) == []


def test_seeded_non_null_cells_rate_is_detected() -> None:
    seeded = "def rate(fmt):\n    return fmt.null_count / (fmt.null_count + fmt.non_null_cells)\n"
    assert violations("generation/statistical/_sample.py", seeded)
