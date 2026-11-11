"""AT-6: the generate-option registry and the generators' reads name the same keys.

An AST scan of the files that read a generate column collects every literal key read with
``.get("<k>")`` or ``["<k>"]`` on a column variable. Every collected key must be in the registry
(a valid config would be refused otherwise) and every registry key must be collected (a typo-shaped
key would be accepted otherwise). The scan follows the variable names in ``COLUMN_VARS``; a reader
that uses another name, or reads through a helper, adds the name there. A reader file with no
match fails, so a rename cannot empty the scan.

Keys are compared per type where the read sits in a ``kind == "<type>"`` branch and as "any type"
otherwise; the registry side is the union of all types plus the common and internal keys.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from decoy_engine.config._generate_options import (
    COMMON_OPTIONS,
    GENERATE_OPTIONS,
    INTERNAL_KEYS,
)

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"

COLUMN_VARS = frozenset({"col", "cfg", "column_config", "col_cfg"})
# Declared fields and the column's own identity, not options.
DECLARED = frozenset({"name", "type", "pooled"})

READER_FILES = (
    "generation/synthesize.py",
    "generation/_synthesize_derived.py",
    "generation/_column_streams.py",
    "generation/_bounds.py",
    "generation/_admission.py",
    "generation/_reproducibility.py",
    "generation/_window_admission.py",
    "generation/_pinned_compile.py",
    "generation/_faker_pool.py",
    "generation/_grouped_windowed_generators.py",
    "generation/kinds/_constant.py",
    "generation/kinds/_output_type.py",
    "generation/kinds/_parametric.py",
    "generation/_referenced_formula.py",
    "generators/columns.py",
    "generators/derivation.py",
    "generators/_formula.py",
    "generation/statistical/_spec.py",
)
# Transform parsers read the column dict as `cfg`, but only inside `from_dict`.
FROM_DICT_FILES = (
    "transforms/derived.py",
    "transforms/derived_aggregate.py",
    "transforms/grouped_series.py",
    "transforms/windowed_date.py",
    "transforms/group_key.py",
)


def _receiver(node: ast.expr) -> str | None:
    return node.id if isinstance(node, ast.Name) else None


def _reads(tree: ast.AST) -> set[tuple[str | None, str]]:
    """(type branch or None, key) for each literal read on a column variable."""
    found: set[tuple[str | None, str]] = set()

    def visit(node: ast.AST, branch: str | None) -> None:
        if isinstance(node, ast.If):
            tag = _branch_type(node.test)
            visit(node.test, branch)
            for child in node.body:
                visit(child, tag or branch)
            for child in node.orelse:
                visit(child, branch)
            return
        key = _literal_key(node)
        if key is not None:
            found.add((branch, key))
        for child in ast.iter_child_nodes(node):
            visit(child, branch)

    visit(tree, None)
    return found


def _branch_type(test: ast.expr) -> str | None:
    """The type literal in ``kind == "x"`` or ``col.get("type") == "x"``, else None."""
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Constant)
        and isinstance(test.comparators[0].value, str)
        and test.comparators[0].value in GENERATE_OPTIONS
    ):
        left = test.left
        if isinstance(left, ast.Name) and left.id in {"kind", "data_type"}:
            return str(test.comparators[0].value)
        if _literal_key(left) == "type":
            return str(test.comparators[0].value)
    return None


def _literal_key(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and _receiver(node.func.value) in COLUMN_VARS
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _receiver(node.value) in COLUMN_VARS
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def _from_dict_tree(path: Path) -> ast.AST:
    tree = ast.parse(path.read_text())
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "from_dict"]
    assert funcs, f"{path.name}: no from_dict to scan"
    return ast.Module(body=funcs, type_ignores=[])


def _collect() -> dict[str, set[tuple[str | None, str]]]:
    out: dict[str, set[tuple[str | None, str]]] = {}
    for rel in READER_FILES:
        out[rel] = _reads(ast.parse((SRC / rel).read_text()))
    for rel in FROM_DICT_FILES:
        out[rel] = _reads(_from_dict_tree(SRC / rel))
    return out


READS = _collect()


def _allowed(type_: str | None) -> set[str]:
    base = set(COMMON_OPTIONS) | set(INTERNAL_KEYS) | set(DECLARED)
    if type_ is None:
        for opts in GENERATE_OPTIONS.values():
            base |= set(opts.keys)
        return base
    return base | set(GENERATE_OPTIONS[type_].keys)


@pytest.mark.parametrize("rel", sorted(READS))
def test_reader_file_has_matches(rel: str) -> None:
    assert READS[rel], f"{rel}: the scan found no column reads; was a variable renamed?"


@pytest.mark.parametrize("rel", sorted(READS))
def test_every_read_key_is_in_the_registry(rel: str) -> None:
    missing = sorted(
        f"{branch or 'any'}:{key}" for branch, key in READS[rel] if key not in _allowed(branch)
    )
    assert not missing, f"{rel} reads keys the registry does not list: {missing}"


def test_every_registry_key_is_read_somewhere() -> None:
    read = {key for reads in READS.values() for _, key in reads}
    unread = {
        f"{type_}.{key}"
        for type_, opts in GENERATE_OPTIONS.items()
        for key in opts.keys
        if key not in read
    }
    unread |= {f"common.{key}" for key in COMMON_OPTIONS if key not in read}
    unread |= {f"internal.{key}" for key in INTERNAL_KEYS if key not in read}
    assert not unread, f"registry keys no generator reads: {sorted(unread)}"


def test_internal_key_list_starts_empty() -> None:
    assert not INTERNAL_KEYS
