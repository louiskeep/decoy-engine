"""Sentry: every public run entry calls `refuse_unapplied_subset` (or is named as exempt).

A sixth entry that takes a pipeline config and runs or compiles it must not ship
unguarded, or a `subset:` block is silently ignored again.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from collections.abc import Callable

import decoy_engine
from decoy_engine import execution
from decoy_engine.execution import native

# Entries that take a config and must call the guard in their own body.
GUARDED = {
    "run_pipeline",
    "run_pipeline_isolated",
    "run_mask_pipeline_chunked",
    "run_mask_chunked",
    "run_native_or_oracle_chunked",
    "compile_plan",
}
# Guarded entries whose pre-read sequence lives once, in the guard-free core they return (the
# resident auto-chunk lane calls the same core): the entry calls the core, the core the guard.
IN_CORE = {
    "run_mask_pipeline_chunked": (
        "decoy_engine.execution._chunked_oracle",
        "_mask_pipeline_chunked",
    ),
    "run_mask_chunked": ("decoy_engine.execution.native.chunked._core", "mask_chunked"),
}
# Entries that delegate to a guarded one before any read or spawn.
DELEGATING = {
    "run_job_with_governor": "every rung runs through run_pipeline_isolated",
    "run_config_only_checks": "validates the config only; it neither reads a source nor compiles a plan",
}


def _public_callables() -> dict[str, Callable[..., object]]:
    found: dict[str, Callable[..., object]] = {}
    for module in (decoy_engine, execution, native):
        for name in getattr(module, "__all__", ()):
            obj = getattr(module, name, None)
            if callable(obj) and (name.startswith(("run_", "compile_"))):
                found[name] = obj
    return found


def _calls_guard(fn: Callable[..., object]) -> bool:
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(fn)))
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "refuse_unapplied_subset":
                return True
    return False


def _guard_holder(name: str, fn: Callable[..., object]) -> Callable[..., object]:
    """The function that must call the guard: the entry itself, or the core it returns."""
    if name not in IN_CORE:
        return fn
    module, core = IN_CORE[name]
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(fn)))
    returned = [
        n.value.func
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Call)
    ]
    assert any(isinstance(f, ast.Attribute) and f.attr == core for f in returned), (
        f"{name} does not return its core {core}"
    )
    return getattr(importlib.import_module(module), core)


def test_every_guarded_entry_calls_the_guard() -> None:
    entries = _public_callables()
    for name in sorted(GUARDED):
        assert name in entries, f"{name} is no longer exported; update GUARDED"
        holder = _guard_holder(name, entries[name])
        assert _calls_guard(holder), f"{name} does not call refuse_unapplied_subset"


def test_no_public_run_entry_is_unclassified() -> None:
    for name, fn in sorted(_public_callables().items()):
        params = list(inspect.signature(fn).parameters)
        if not params or params[0] != "config":
            continue
        assert name in GUARDED or name in DELEGATING, (
            f"public entry {name} takes a config; guard it with refuse_unapplied_subset "
            "or name it in DELEGATING with the reason"
        )
