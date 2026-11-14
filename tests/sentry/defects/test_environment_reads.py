"""One engine parser for boolean environment flags.

A flag is read through ``decoy_engine.internal.env_flags.env_flag``, which owns the accepted
spellings, the default and the value-free refusal. A second reading of an environment variable
as a flag (a comparison, a membership test, ``bool(...)`` or a bare truthiness test on the
read) is a second rule for the same surface and fails here.

Reads that are not flags stay: a value assigned to a name (a path, a locator, a signing key
checked for "non-empty") and a mapping handed on whole (``dict(os.environ)``).

Pure AST. Spellings resolved: ``os.environ``, ``os.getenv``, ``import os as o``,
``from os import environ as e`` and ``from os import getenv as g``, and a plain re-binding of
either. Deliberate evasion (``getattr``, a read assigned to a name and compared later) is out of
scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.sentry._call_resolution import local_names
from tests.sentry.defects import _defect_scan as scan

_STRING_STEPS = frozenset({"strip", "lstrip", "rstrip", "lower", "upper", "casefold"})
_OWNER = "decoy_engine/internal/env_flags.py"
_FLAG_CALLERS = {
    "decoy_engine/profiling/model_pack/loader.py": "env_flag",
    "decoy_engine/execution/_isolated_run.py": "env_flag",
}


def _is_environ(node: ast.AST, names: set[str]) -> bool:
    return (isinstance(node, ast.Name) and node.id in names) or (
        isinstance(node, ast.Attribute) and node.attr == "environ"
    )


def _is_env_read(node: ast.AST, environ: set[str], getenv: set[str]) -> bool:
    if isinstance(node, ast.Subscript):
        return _is_environ(node.value, environ)
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in getenv
    if isinstance(func, ast.Attribute):
        return func.attr == "getenv" or (func.attr == "get" and _is_environ(func.value, environ))
    return False


def _flag_use(read: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when the read is compared, coerced with ``bool`` or truth-tested in place."""
    node: ast.AST = read
    while True:
        parent = parents.get(node)
        if (
            isinstance(parent, ast.Attribute)
            and parent.attr in _STRING_STEPS
            and isinstance(parents.get(parent), ast.Call)
        ):
            node = parents[parent]
            continue
        break
    if isinstance(parent, ast.Compare):
        return True
    if isinstance(parent, ast.Call):
        return isinstance(parent.func, ast.Name) and parent.func.id == "bool" and node in parent.args
    if isinstance(parent, ast.UnaryOp):
        return isinstance(parent.op, ast.Not)
    if isinstance(parent, (ast.If, ast.While, ast.IfExp)):
        return parent.test is node
    return False


def flag_reads(tree: ast.AST, rel: str) -> list[str]:
    """One ``path::function::ordinal`` key per environment read used as a flag."""
    environ = local_names(tree, "environ")
    getenv = local_names(tree, "getenv")
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: list[str] = []
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        if not _is_env_read(node, environ, getenv) or not _flag_use(node, parents):
            continue
        owner = parents.get(node)
        while owner is not None and not isinstance(
            owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            owner = parents.get(owner)
        name = getattr(owner, "name", "<module>")
        ordinal = counts.get(name, 0)
        counts[name] = ordinal + 1
        found.append(f"{rel}::{name}::{ordinal}")
    return sorted(found)


def _scan_source(source: str) -> list[str]:
    return flag_reads(ast.parse(source), "decoy_engine/x.py")


# ----- the real tree ------------------------------------------------------------------------


def test_no_environment_flag_is_read_outside_the_one_parser() -> None:
    found: list[str] = []
    for path in scan.source_files():
        rel = f"decoy_engine/{path.relative_to(scan.SRC).as_posix()}"
        if rel == _OWNER:
            continue
        found += flag_reads(ast.parse(path.read_text(encoding="utf-8")), rel)
    assert not found, f"environment flags must go through env_flag(): {found}"


def test_the_named_flag_sites_call_the_one_parser() -> None:
    for rel, callee in _FLAG_CALLERS.items():
        tree = ast.parse((scan.SRC.parent / rel).read_text(encoding="utf-8"))
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert callee in called, f"{rel} no longer reads its flag through {callee}"


def test_the_owner_module_exists() -> None:
    assert (Path(scan.SRC) / "internal" / "env_flags.py").is_file()


# ----- seeded spellings: each must be a hit -------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        'import os\nx = os.environ.get("X") == "1"\n',
        'import os\nx = os.getenv("X", "0") in {"1", "true"}\n',
        'import os\nx = os.environ["X"].strip().lower() == "true"\n',
        'from os import environ as e\nx = e.get("X") != "0"\n',
        'import os\nx = bool(os.environ.get("X"))\n',
        'from os import getenv as g\nx = g("X") == "1"\n',
        'import os as o\nx = o.environ.get("X") == "1"\n',
        'import os\nif os.environ.get("X"):\n    pass\n',
        'import os\nx = not os.environ.get("X")\n',
        'import os\nenv = os.environ\nx = env.get("X") == "1"\n',
        'import os\nx = 1 if os.environ.get("X") else 2\n',
        'import os\nx = os.environ.get("X", "").lower() not in ("", "0")\n',
    ],
)
def test_a_flag_shaped_read_is_a_hit(source: str) -> None:
    assert len(_scan_source(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        'import os\nroot = os.environ.get("X", "/tmp")\n',
        'import os\nraw = os.environ.get("X")\n',
        'import os\nenv = {**os.environ, "X": "1"}\n',
        'import os\nenv = dict(os.environ)\n',
        'import os\nok = key is None and os.environ.get("K", "").strip()\n',
        'def f(environ):\n    return environ is None\n',
    ],
)
def test_a_non_flag_read_is_not_a_hit(source: str) -> None:
    assert _scan_source(source) == []


def test_ordinals_count_per_function() -> None:
    src = (
        "import os\n"
        'def f():\n    return os.environ.get("A") == "1" or os.environ.get("B") == "1"\n'
    )
    assert _scan_source(src) == ["decoy_engine/x.py::f::0", "decoy_engine/x.py::f::1"]
