"""Which columns a compiled expression reads has one owner.

`expressions.column_refs` (`expressions/_lark_parser.py`) walks the parse tree for `col_ref`
nodes. The derived-column plan check and the generate-input resolver each used to walk it
themselves; a grammar change that taught one of them a new reference node would leave the
other blind, so a derived column could pass the existence check yet get no ordering edge.

Scope: a function anywhere under `src/decoy_engine` outside the owner module that holds the
`"col_ref"` node name and calls a Lark tree walk (`iter_subtrees`, `iter_subtrees_topdown`,
`find_data`, `find_pred`, `scan_values`). A dispatch on the node kind that walks nothing (the
evaluator's transformer, numeric typing's type rule) is not a walk and is not flagged. Pure AST:
nothing scanned is imported.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
OWNER = "expressions/_lark_parser.py"
NODE = "col_ref"
WALKS = frozenset(
    {"iter_subtrees", "iter_subtrees_topdown", "find_data", "find_pred", "scan_values"}
)


def _functions(tree: ast.AST) -> list[ast.AST]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    ]


def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    """The nodes of `fn` that are not inside a nested function or lambda."""
    found: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(fn))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return found


def column_ref_walks(sources: dict[str, str]) -> list[str]:
    """`file:line` of each function outside the owner that walks a parse tree for `col_ref`."""
    found: list[str] = []
    for rel, text in sorted(sources.items()):
        if rel == OWNER:
            continue
        for fn in _functions(ast.parse(text)):
            nodes = _own_nodes(fn)
            names = any(isinstance(n, ast.Constant) and n.value == NODE for n in nodes)
            walks = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in WALKS
                for n in nodes
            )
            if names and walks:
                found.append(f"{rel}:{fn.lineno}")
    return found


def _sources() -> dict[str, str]:
    return {p.relative_to(SRC).as_posix(): p.read_text(encoding="utf-8") for p in SRC.rglob("*.py")}


def test_only_the_owner_walks_a_parse_tree_for_column_references() -> None:
    assert column_ref_walks(_sources()) == []


def test_the_owner_defines_the_public_extractor() -> None:
    tree = ast.parse((SRC / OWNER).read_text(encoding="utf-8"))
    assert "column_refs" in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_meta_each_walk_shape_is_seen() -> None:
    shapes = {
        "comprehension": (
            "def refs(c):\n"
            "    return {str(t.children[0]) for t in c.tree.iter_subtrees() if t.data == 'col_ref'}\n"
        ),
        "loop": (
            "def refs(c):\n"
            "    out = set()\n"
            "    for t in c.tree.iter_subtrees():\n"
            "        if t.data == 'col_ref':\n"
            "            out.add(t.children[0])\n"
            "    return out\n"
        ),
        "nested": (
            "def outer(e, x):\n"
            "    def has(node):\n"
            "        return any(t.data == 'col_ref' for t in node.iter_subtrees_topdown())\n"
            "    return has(e)\n"
        ),
        "find_data": "def refs(tree):\n    return [t.children[0] for t in tree.find_data('col_ref')]\n",
        "lambda": "has = lambda n: any(t.data == 'col_ref' for t in n.iter_subtrees())\n",
    }
    for label, text in shapes.items():
        assert len(column_ref_walks({"plan/_rogue.py": text})) == 1, label


def test_meta_a_dispatch_or_an_unrelated_walk_is_not_seen() -> None:
    clean = (
        "def typ(n):\n"
        "    if n.data == 'col_ref':\n"
        "        return 'num'\n"
        "def strings(tree):\n"
        "    return [t for t in tree.iter_subtrees() if t.data == 'string_lit']\n"
    )
    assert column_ref_walks({"generation/_rogue.py": clean}) == []
    walk = "def refs(c):\n    return [t for t in c.iter_subtrees() if t.data == 'col_ref']\n"
    assert column_ref_walks({OWNER: walk}) == []
