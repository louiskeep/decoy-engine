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
    "plan/checks/_lookup_tables.py",
    "generation/kinds/_parametric.py",
    "generation/_referenced_formula.py",
    "generation/relational/_self_reference.py",
    "generators/columns.py",
    "generators/derivation.py",
    "generators/_formula.py",
    "generation/statistical/_spec.py",
    "generation/relational/_cardinality.py",
    # The one reader of the per-parent bounds every generation route goes through.
    "config/_generate_relational.py",
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


# Packages that read a generate column. A per-parent bound is read only through
# `config.per_parent_bounds`, so validate, the probe, admission and both draws agree on what counts
# as a bound.
BOUND_READER_ROOTS = ("config", "generation", "generators", "execution")
BOUND_KEYS = frozenset({"min_per_parent", "max_per_parent"})
# (file, top-level definition) that may name the keys: the one reader and the registry entries.
BOUND_KEY_OWNERS = frozenset(
    {
        ("config/_generate_relational.py", "per_parent_bounds"),
        ("config/_generate_options.py", "_BASE_OPTIONS"),
        ("config/_generate_options.py", "_NONE_MEANS_ABSENT"),
    }
)


def _top_level_names(stmt: ast.stmt) -> set[str]:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {stmt.name}
    targets = stmt.targets if isinstance(stmt, ast.Assign) else []
    if isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
    return {t.id for t in targets if isinstance(t, ast.Name)}


def _bound_key_uses(tree: ast.Module, owners: frozenset[str] = frozenset()) -> list[int]:
    """Lines where a bound key, outside `owners`, is used to read a column.

    A read passes the key to a call (`col.get(k)`, a helper), subscripts with it, compares with
    it, or lists it in a container that is iterated or named (a key tuple read in a loop). A key
    that only labels a problem location, or is the target of a write, reads nothing.
    """
    skipped: set[int] = set()
    for stmt in tree.body:
        if _top_level_names(stmt) & owners:
            skipped |= {id(node) for node in ast.walk(stmt)}
    keys: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            keys += [*node.args, *(kw.value for kw in node.keywords)]
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            keys.append(node.slice)
        elif isinstance(node, ast.Compare):
            keys += [node.left, *node.comparators]
        elif isinstance(node, (ast.For, ast.comprehension)) and isinstance(
            node.iter, (ast.Tuple, ast.List, ast.Set)
        ):
            keys += node.iter.elts
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
            node.value, (ast.Tuple, ast.List, ast.Set)
        ):
            keys += node.value.elts
    return sorted(
        key.lineno
        for key in keys
        if isinstance(key, ast.Constant) and key.value in BOUND_KEYS and id(key) not in skipped
    )


def _owners_of(rel: str) -> frozenset[str]:
    return frozenset(name for path, name in BOUND_KEY_OWNERS if path == rel)


def test_per_parent_bounds_are_read_only_through_the_config_helper() -> None:
    found = set()
    for root in BOUND_READER_ROOTS:
        for path in sorted((SRC / root).rglob("*.py")):
            rel = path.relative_to(SRC).as_posix()
            for line in _bound_key_uses(ast.parse(path.read_text()), _owners_of(rel)):
                found.add(f"{rel}:{line}")
    assert not found, f"read the bounds through config.per_parent_bounds instead: {sorted(found)}"


def test_every_bound_key_owner_names_a_real_definition() -> None:
    for rel, name in sorted(BOUND_KEY_OWNERS):
        tree = ast.parse((SRC / rel).read_text())
        assert any(name in _top_level_names(stmt) for stmt in tree.body), f"{rel}: no {name}"


def test_the_bound_scan_sees_a_direct_read() -> None:
    tree = ast.parse(
        'lo = col.get("min_per_parent")\n'
        'col["max_per_parent"] = 3\n'
        'f(c, "max_per_parent")\n'
        'KEYS = ("min_per_parent",)\n'
        'def g():\n    return ("max_per_parent", 1)\n'
        'def h():\n    return col["min_per_parent"], 0\n'
        'for k in ("max_per_parent",):\n    pass\n'
        'p.append((("min_per_parent",), 1))\n'
        'b = k == "max_per_parent"\n'
    )
    assert _bound_key_uses(tree) == [1, 3, 4, 8, 9, 12]
    assert _bound_key_uses(tree, frozenset({"KEYS", "h"})) == [1, 3, 9, 12]
