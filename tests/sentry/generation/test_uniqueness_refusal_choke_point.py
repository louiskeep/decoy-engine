"""AT-10 (GEN-UNIQUE): `uniqueness_impossible` is raised only by `raise_uniqueness_impossible`.

The refusal builder takes names and counts and nothing else, so no caller can format a generated
or parent value into the message. Two checks keep that true:

1. ``GenerationError(code=<uniqueness_impossible>)`` is constructed only inside the builder, in
   `generation/pool/_errors.py`. The code is recognised as the registry constant
   (``UNIQUENESS_IMPOSSIBLE``) or its literal text, through any attribute path or alias.
2. Every caller that raises the refusal calls the builder: the masking sampler today; the
   generation draw module joins `CALLERS` when it lands.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
BUILDER = "raise_uniqueness_impossible"
BUILDER_FILE = "generation/pool/_errors.py"
CALLERS = ("generation/pool/_sampler.py",)
CODE_NAMES = ("UNIQUENESS_IMPOSSIBLE", "uniqueness_impossible")


def _mentions_code(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr.upper() == CODE_NAMES[0]:
            return True
        if isinstance(sub, ast.Name) and sub.id.upper() == CODE_NAMES[0]:
            return True
        if isinstance(sub, ast.Constant) and sub.value == CODE_NAMES[1]:
            return True
    return False


def _callee(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _error_names(tree: ast.AST) -> frozenset[str]:
    """`GenerationError` plus every local alias from `from ... import GenerationError as GE`."""
    names = {"GenerationError"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname for a in node.names if a.name == "GenerationError" and a.asname)
    return frozenset(names)


def refusal_constructions(source: str) -> list[tuple[int, str]]:
    """(line, enclosing function) of each GenerationError built with the uniqueness code."""
    tree = ast.parse(source)
    error_names = _error_names(tree)
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(fn):
                owner[id(child)] = fn.name
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node) in error_names:
            code_args = [kw.value for kw in node.keywords if kw.arg == "code"] + list(node.args[:1])
            if any(_mentions_code(a) for a in code_args):
                out.append((node.lineno, owner.get(id(node), "")))
    return out


def calls_builder(source: str) -> bool:
    return any(
        isinstance(n, ast.Call) and _callee(n) == BUILDER for n in ast.walk(ast.parse(source))
    )


def test_only_the_builder_constructs_the_refusal():
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for line, fn in refusal_constructions(path.read_text(encoding="utf-8")):
            if (rel, fn) != (BUILDER_FILE, BUILDER):
                bad.append(f"{rel}:{line} builds the uniqueness refusal in {fn or '<module>'}")
    assert bad == []


def test_the_builder_is_found_so_the_scan_cannot_go_empty():
    text = (SRC / BUILDER_FILE).read_text(encoding="utf-8")
    assert [fn for _, fn in refusal_constructions(text)] == [BUILDER]


@pytest.mark.parametrize("rel", CALLERS)
def test_each_caller_calls_the_builder(rel):
    assert calls_builder((SRC / rel).read_text(encoding="utf-8"))


def test_builder_is_exported_from_the_pool_package():
    text = (SRC / "generation/pool/__init__.py").read_text(encoding="utf-8")
    assert f'"{BUILDER}"' in text


@pytest.mark.parametrize(
    "snippet",
    [
        "raise GenerationError(code=reasons.runtime.UNIQUENESS_IMPOSSIBLE, message='x')",
        "raise GenerationError(code='uniqueness_impossible', message=m)",
        "raise GenerationError(code=UNIQUENESS_IMPOSSIBLE)",
        "raise GenerationError(code=str(reasons.runtime.UNIQUENESS_IMPOSSIBLE))",
        "raise errors.GenerationError(reasons.runtime.UNIQUENESS_IMPOSSIBLE, 'x')",
    ],
)
def test_scanner_catches_each_spelling(snippet):
    # M1: a bypass of the builder must be seen.
    assert refusal_constructions(snippet)


def test_scanner_catches_an_aliased_constructor():
    # M2: `from ... import GenerationError as GE` must not hide a bypass.
    snippet = (
        "from decoy_engine.errors import GenerationError as GE\n"
        "def f():\n    raise GE(code='uniqueness_impossible', message='x')"
    )
    assert refusal_constructions(snippet)


def test_scanner_ignores_other_codes():
    assert not refusal_constructions("raise GenerationError(code=reasons.runtime.DTYPE_UNSAFE)")
