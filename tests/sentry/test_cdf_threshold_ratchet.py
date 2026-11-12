"""Ratchet: no float accumulation feeds a categorical CDF threshold.

The categorical CDF builder must stay integer-exact (a float `sum()` made thresholds
interpreter-dependent on CPython 3.12). This scan reads every module under
`decoy_engine/execution` with `ast` and, inside any function that references
`_WEIGHTED_CDF_RES`, flags a call to `sum` or `fsum` and any true division (`/`), the
shapes of the old `running / total * RES` builder.

Limits: this catches the accidental shapes only. It does not prove that a differently
written float builder is absent (a helper that does the float work without naming
`_WEIGHTED_CDF_RES`, `operator.truediv`, `numpy.cumsum`, or a call through an alias all
pass it). The exact behaviour is pinned by `tests/unit/execution/test_categorical_cdf_exact.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import decoy_engine

EXECUTION_ROOT = Path(decoy_engine.__file__).parent / "execution"
_RES_NAME = "_WEIGHTED_CDF_RES"
_SUM_NAMES = {"sum", "fsum"}


def _names_res(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == _RES_NAME:
            return True
        if isinstance(node, ast.Attribute) and node.attr == _RES_NAME:
            return True
    return False


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def violations(source: str, label: str = "<src>") -> list[str]:
    found: list[str] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _names_res(fn):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and _call_name(node) in _SUM_NAMES:
                found.append(f"{label}:{node.lineno} {fn.name}: {_call_name(node)}()")
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                found.append(f"{label}:{node.lineno} {fn.name}: true division")
            if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Div):
                found.append(f"{label}:{node.lineno} {fn.name}: /=")
    return found


# The pre-fix builder, verbatim in shape: the scan must flag it.
_FLOAT_BUILDER_SEED = """
def _build_cdf(weights):
    total = sum(weights)
    cdf = []
    running = 0.0
    for w in weights:
        running += w
        cdf.append(int(running / total * _WEIGHTED_CDF_RES))
    return cdf
"""


def test_execution_tree_has_no_float_threshold_accumulation() -> None:
    modules = sorted(EXECUTION_ROOT.rglob("*.py"))
    assert len(modules) > 50  # the scan reads the real tree, not an empty directory
    found: list[str] = []
    for path in modules:
        found += violations(path.read_text(encoding="utf-8"), str(path.relative_to(EXECUTION_ROOT)))
    assert found == []


def test_seeded_float_builder_is_flagged() -> None:
    found = violations(_FLOAT_BUILDER_SEED)
    assert any("sum()" in f for f in found)
    assert any("true division" in f for f in found)


def test_seeds_each_shape_on_its_own() -> None:
    assert violations("def f(w):\n    return math.fsum(w) + _WEIGHTED_CDF_RES\n")
    assert violations("def f(c, t):\n    x = c\n    x /= t\n    return x * _WEIGHTED_CDF_RES\n")
    assert violations("def f(c, t):\n    return int(c / t * mod._WEIGHTED_CDF_RES)\n")


def test_functions_without_the_resolution_are_out_of_scope() -> None:
    assert violations("def f(w):\n    return sum(w) / len(w)\n") == []
    assert violations("def f(c, t):\n    return (2 * c * _WEIGHTED_CDF_RES + t) // (2 * t)\n") == []
