"""Sentry: every non-resident execution entry refuses model post-scan through the one guard.

A route that runs no post-scan must not mask under a config that asks for model post-scan
detectors or `ner_findings: fail` (SCAN-ML M5). Three rules hold that:

1. Entry list. Every public or `@entry_point` execution callable that takes `config` is either
   GUARDED (its first statement after the docstring and local imports is
   `refuse_unchecked_route(config, ...)`) or EXEMPT with a reason. A new config-taking entry fails
   until it is placed.
2. Callers. Every product call (under `src/`) of a plan-level entry, or of a wrapper that forwards
   to one, passes `config=`.
3. Resident lane. `run_pipeline`'s own modules (`execution/_pipeline*.py`) never call a guarded
   public chunked entry: its auto-chunk lane runs the post-scan, so it uses the guard-free cores.

Rules 2 and 3 see a call under an import alias, a re-binding or an attribute, through the
resolver the declaration-gate sentry uses (`tests/sentry/_call_resolution.py`).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from pathlib import Path

import pytest

from tests.sentry._call_resolution import resolved_calls

SRC = Path(__file__).resolve().parents[2] / "src"
EXECUTION = SRC / "decoy_engine" / "execution"
GUARD = "refuse_unchecked_route"
PACKAGES = (
    "decoy_engine",
    "decoy_engine.execution",
    "decoy_engine.execution.native",
    "decoy_engine.execution.out_of_core",
)

GUARDED: dict[str, str] = {
    "decoy_engine.execution._sequential.run_sequential": "sequential FK route",
    "decoy_engine.execution.out_of_core._runner.run_fk_out_of_core": "out-of-core FK route",
    "decoy_engine.execution._chunked.run_mask_pipeline_chunked": "oracle chunked route",
    "decoy_engine.execution.native._chunked_entry.run_mask_chunked": "native chunked route",
    "decoy_engine.execution.native._dispatch.run_native_or_oracle_chunked": "legacy chunked driver",
}
EXEMPT: dict[str, str] = {
    "decoy_engine.execution._pipeline.run_pipeline": (
        "runs the post-scan on its resident routes and keeps integrity runs off the others"
    ),
    "decoy_engine.execution._isolated_run.run_pipeline_isolated": "runs run_pipeline in a child",
    "decoy_engine.execution._governor.run_job_with_governor": "runs run_pipeline",
    "decoy_engine.execution._chunked.check_chunked_compatibility": "admission check, masks nothing",
    "decoy_engine.execution._pipeline_context.classify_table_kinds": "reads config names only",
    "decoy_engine.execution._planner.classify_job": "route classification, masks nothing",
    "decoy_engine.execution._probe.probe_peak_bytes": "memory probe, publishes no output",
    "decoy_engine.execution._transforms_table.apply_table_transforms": "transforms, not masking",
    "decoy_engine.execution.native._dispatch.plan_native_route": "routing decision only",
    "decoy_engine.execution.out_of_core._spill_estimate.predict_ooc_disk_bytes": "disk estimate, masks nothing",
}
# Plan-level entries and the wrappers that forward to them; each product call passes `config=`.
CONFIG_CALLEES = frozenset(
    {
        "run_sequential",
        "run_fk_out_of_core",
        "run_sequential_route",
        "run_out_of_core_route",
    }
)
PUBLIC_CHUNKED = frozenset(
    {"run_mask_chunked", "run_mask_pipeline_chunked", "run_native_or_oracle_chunked"}
)


def _qualname(fn: object) -> str:
    target = inspect.unwrap(fn)  # type: ignore[arg-type]
    return f"{target.__module__}.{target.__qualname__}"


def _decorated_entry_points() -> set[str]:
    found: set[str] = set()
    for path in sorted(EXECUTION.rglob("*.py")):
        module = ".".join(path.relative_to(SRC).with_suffix("").parts)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(d, ast.Name) and d.id == "entry_point" for d in node.decorator_list
            ):
                found.add(f"{module}.{node.name}")
    return found


def config_taking_entries() -> set[str]:
    names = _decorated_entry_points()
    for package in PACKAGES:
        module = importlib.import_module(package)
        for name in getattr(module, "__all__", ()):
            value = getattr(module, name, None)
            if callable(value) and not inspect.isclass(value):
                names.add(_qualname(value))
    taking: set[str] = set()
    for qualified in names:
        module_name, _, attr = qualified.rpartition(".")
        if not module_name.startswith("decoy_engine.execution"):
            continue
        value = getattr(importlib.import_module(module_name), attr)
        if "config" in inspect.signature(value).parameters:
            taking.add(qualified)
    return taking


def _is_vault_refusal(node: ast.stmt) -> bool:
    """The B0 statement `if ...: raise vault_requires_resident_output()`: it reads no argument
    but the writer and sink, so it may precede the guard."""
    if not isinstance(node, ast.If) or len(node.body) != 1 or node.orelse:
        return False
    stmt = node.body[0]
    return (
        isinstance(stmt, ast.Raise)
        and isinstance(stmt.exc, ast.Call)
        and isinstance(stmt.exc.func, ast.Name)
        and stmt.exc.func.id == "vault_requires_resident_output"
    )


def guard_first(function: ast.FunctionDef) -> bool:
    """The first statement after the docstring, local imports and the B0 vault refusal is
    `refuse_unchecked_route(config, ...)`."""
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    while body and (isinstance(body[0], ast.Import | ast.ImportFrom) or _is_vault_refusal(body[0])):
        body = body[1:]
    if not body or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Call):
        return False
    call = body[0].value
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == GUARD
        and bool(call.args)
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "config"
    )


def _function_node(qualified: str) -> ast.FunctionDef:
    module_name, _, name = qualified.rpartition(".")
    path = Path(importlib.import_module(module_name).__file__ or "")
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{qualified} is not a module-level function")


def test_every_config_taking_execution_entry_is_placed() -> None:
    taking = config_taking_entries()
    unplaced = sorted(taking - set(GUARDED) - set(EXEMPT))
    assert unplaced == [], f"place each in GUARDED or EXEMPT: {unplaced}"
    assert set(GUARDED) <= taking, sorted(set(GUARDED) - taking)
    assert not set(GUARDED) & set(EXEMPT)


@pytest.mark.parametrize("qualified", sorted(GUARDED))
def test_guarded_entry_calls_the_guard_first(qualified: str) -> None:
    assert guard_first(_function_node(qualified)), f"{qualified} does not call {GUARD} first"


def _watched_calls(source: str, targets: frozenset[str]) -> list[tuple[str, ast.Call]]:
    """Calls of each target under any local spelling (see `tests/sentry/_call_resolution.py`)."""
    tree = ast.parse(source)
    found = [(target, call) for target in sorted(targets) for call in resolved_calls(tree, target)]
    return sorted(found, key=lambda hit: (hit[1].lineno, hit[1].col_offset))


def calls_without_config(source: str) -> list[str]:
    return [
        f"{target}:{call.lineno}"
        for target, call in _watched_calls(source, CONFIG_CALLEES)
        if "config" not in {k.arg for k in call.keywords}
    ]


def test_every_product_call_of_a_plan_level_entry_passes_config() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{hit}"
        for path in sorted((SRC / "decoy_engine").rglob("*.py"))
        for hit in calls_without_config(path.read_text())
    ]
    assert offenders == []


def resident_lane_public_calls(source: str) -> list[str]:
    return [f"{target}:{call.lineno}" for target, call in _watched_calls(source, PUBLIC_CHUNKED)]


def test_run_pipeline_modules_never_call_a_guarded_chunked_entry() -> None:
    lane = sorted(EXECUTION.glob("_pipeline*.py"))
    assert EXECUTION / "_pipeline_auto_chunk.py" in lane
    offenders = [
        f"{path.name}:{hit}"
        for path in lane
        for hit in resident_lane_public_calls(path.read_text())
    ]
    assert offenders == []


# -- seeded mutants: each checker fails the way it is meant to ---------------------------------


def _fn(source: str) -> ast.FunctionDef:
    node = ast.parse(textwrap.dedent(source)).body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


@pytest.mark.parametrize(
    ("source", "ok"),
    [
        ('def f(config):\n    """Doc."""\n    refuse_unchecked_route(config, "r")\n', True),
        ("def f(config):\n    from m import g\n    refuse_unchecked_route(config, 'r')\n", True),
        ("def f(config):\n    other(config)\n    refuse_unchecked_route(config, 'r')\n", False),
        ("def f(config):\n    refuse_unchecked_route({}, 'r')\n", False),
        ("def f(config):\n    pass\n", False),
    ],
)
def test_guard_first_checker_catches_a_late_or_missing_guard(source: str, ok: bool) -> None:
    assert guard_first(_fn(source)) is ok


def test_caller_checker_catches_an_omitted_config() -> None:
    source = "run_sequential(a, p, l, registry=r)\nx.run_fk_out_of_core(p, s, config=c)\n"
    assert calls_without_config(source) == ["run_sequential:1"]


def test_resident_lane_checker_catches_a_public_entry_call() -> None:
    assert resident_lane_public_calls("_chunked_entry.run_mask_chunked(c, s)\n") == [
        "run_mask_chunked:1"
    ]


# One case per spelling of a watched call: an import alias, an attribute call, a plain import.
SPELLINGS = {
    "import_as": "from m import {name} as alias\nalias({args})\n",
    "module_import_as": "import m as mod\nmod.{name}({args})\n",
    "attribute": "import m\nm.{name}({args})\n",
    "from_import": "from m import {name}\n{name}({args})\n",
}


@pytest.mark.parametrize("spelling", sorted(SPELLINGS))
def test_caller_checker_resolves_each_spelling(spelling: str) -> None:
    source = SPELLINGS[spelling].format(name="run_sequential", args="a, p, l")
    assert calls_without_config(source) == ["run_sequential:2"]
    passing = SPELLINGS[spelling].format(name="run_sequential", args="a, config=c")
    assert calls_without_config(passing) == []


@pytest.mark.parametrize("spelling", sorted(SPELLINGS))
def test_resident_lane_checker_resolves_each_spelling(spelling: str) -> None:
    source = SPELLINGS[spelling].format(name="run_mask_chunked", args="c, s")
    assert resident_lane_public_calls(source) == ["run_mask_chunked:2"]


def test_checkers_follow_a_re_binding_of_an_alias() -> None:
    source = "from m import run_sequential as rs\nalias = rs\nalias(a)\n"
    assert calls_without_config(source) == ["run_sequential:3"]
