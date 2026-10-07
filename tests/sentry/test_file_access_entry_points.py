"""D9 engine entry sentry: a public callable that can reach the policy is a declared entry point.

`decoy_engine.fileaccess.ENTRY_POINTS` lists every public callable whose static call graph
reaches `fileaccess.current()`; each one accepts a keyword-only `file_access=` and is wrapped
by `entry_point`, which installs the policy for the call and keeps it on any iterator the call
returns. The platform's own sentry then requires `file_access=` at each call of these names.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from decoy_engine.fileaccess import ENTRY_POINTS
from tests.sentry.fa.callgraph import build_graph

SRC = Path(__file__).parents[2] / "src"

# Seams the platform never calls directly: their exports reach the policy only through the
# declared entry points that call them.
INTERNAL_PREFIXES = (
    "decoy_engine.execution.native",
    "decoy_engine.execution.out_of_core",
    "decoy_engine.execution.physical",
    "decoy_engine.fileaccess",
)


@pytest.fixture(scope="module")
def graph():  # type: ignore[no-untyped-def]
    return build_graph(SRC)


def _public_names(graph) -> set[str]:  # type: ignore[no-untyped-def]
    public: set[str] = set()
    for module, names in graph.all_exports.items():
        parts = module.split(".")
        if any(p.startswith("_") and p != "__init__" for p in parts):
            continue
        if module.startswith(INTERNAL_PREFIXES):
            continue
        for name in names:
            public.add(graph.index.normalize(f"{module}.{name}"))
    return public


def test_every_public_function_that_reaches_the_policy_is_an_entry_point(graph) -> None:  # type: ignore[no-untyped-def]
    reach = graph.reachers()
    missing = sorted(
        name
        for name in _public_names(graph)
        if name in reach
        and name in graph.funcs
        and not name.startswith(INTERNAL_PREFIXES)
        and name not in ENTRY_POINTS
    )
    assert not missing, (
        f"public callables reaching the file-access policy, not in ENTRY_POINTS: {missing}"
    )


def test_every_entry_point_exists_accepts_the_keyword_and_installs_the_policy() -> None:
    for dotted in ENTRY_POINTS:
        module, _, name = dotted.rpartition(".")
        fn = getattr(importlib.import_module(module), name)
        params = inspect.signature(fn).parameters
        assert "file_access" in params, dotted
        assert params["file_access"].kind is inspect.Parameter.KEYWORD_ONLY, dotted
        assert params["file_access"].default is None, dotted
        assert getattr(fn, "__decoy_entry_point__", False), (
            f"{dotted} is not wrapped by entry_point"
        )


def test_entry_points_are_sorted_and_unique() -> None:
    assert list(ENTRY_POINTS) == sorted(set(ENTRY_POINTS))


def test_an_entry_point_that_returns_an_iterator_goes_through_the_binding_wrapper() -> None:
    """`entry_point` binds any iterator result to the policy it ran under, so no listed
    function can hand back a lazy generator that reads under a different policy later."""
    source = (SRC / "decoy_engine" / "fileaccess" / "_entry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wrapper = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "entry_point"
    )
    calls = {
        c.func.id
        for c in ast.walk(wrapper)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "bind_iterator" in calls
