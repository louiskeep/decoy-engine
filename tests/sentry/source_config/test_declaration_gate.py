"""AT-E2c-6: the declaration gate sits before every reader on the run, isolated and profile routes.

`prepare_declared_sources` is the engine's pre-read boundary for config-origin sources. Two
walks guard it:

- (a) in `profile_source`, `build_run_context` and `run_pipeline_isolated`, a
  `prepare_declared_sources(` call comes before every call that reads, profiles, stages or
  spawns on that route, in the same function body;
- (b) every source-side call of `build_declared_source(`, `build_profile_source(` and
  `read_csv_source(` in `src/decoy_engine` is in `READERS` with a reason. A new reader fails
  until someone lists it and says why it is safe.

Covered shapes: a plain call and an attribute call (`mod.name(...)`). The walk matches the
called name, so a reader imported under another name is not seen.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[3] / "src" / "decoy_engine"
GATE = "prepare_declared_sources"

# (file, function) -> calls that must come after the gate in that function body.
ROUTES: dict[tuple[str, str], frozenset[str]] = {
    ("profile/_source.py", "profile_source"): frozenset(
        {"build_declared_source", "_profile_one_source", "profile_descriptors"}
    ),
    ("execution/_pipeline_context.py", "build_run_context"): frozenset(
        {
            "build_declared_source",
            "_profile_one_source",
            "refuse_duplicate_run_sources",
            "profile_source",
        }
    ),
    ("execution/_isolated_run.py", "run_pipeline_isolated"): frozenset(
        {"_write_payload", "_run_in_process", "_run_isolated"}
    ),
}

READER_NAMES = frozenset({"build_declared_source", "build_profile_source", "read_csv_source"})

# (file, enclosing function, reader) -> why this reader is not a bypass of the gate.
READERS: dict[tuple[str, str, str], str] = {
    ("execution/_guards.py", "refuse_duplicate_run_sources", "build_declared_source"): (
        "header or footer only, and after the gate in build_run_context"
    ),
    ("profile/_declared_columns.py", "source_columns", "read_csv_source"): (
        "the gate itself: a header read with nrows=0"
    ),
    ("profile/_declared_columns.py", "source_columns", "build_declared_source"): (
        "the gate itself: schema discovery for non-csv sources"
    ),
    ("profile/_lazy_csv.py", "_chunks", "read_csv_source"): (
        "chunk iterator behind a built source, reached only after a route's gate"
    ),
    ("profile/_lazy_csv.py", "preflight", "read_csv_source"): (
        "header-only read behind a built source, reached only after a route's gate"
    ),
    ("profile/_readers.py", "build_declared_source", "build_profile_source"): (
        "the factory that builds the reader; its callers are the sites listed here"
    ),
    ("profile/_source.py", "profile_source", "build_declared_source"): (
        "the profile loop, after the gate in the same function (walk a)"
    ),
    ("profiling/_scan_integrity.py", "_open_keys", "build_declared_source"): (
        "scan route: its own entry with its own header and width checks, not a run route"
    ),
    ("profiling/_scan_integrity.py", "_open_keys", "read_csv_source"): (
        "scan route: its own entry, not a run route"
    ),
    ("profiling/_scan_integrity.py", "csv_batches", "read_csv_source"): (
        "scan route: its own entry, not a run route"
    ),
    ("profiling/scan_set.py", "_read_and_profile", "build_declared_source"): (
        "scan route: its own entry, not a run route"
    ),
    ("profiling/scan_set.py", "_read_and_profile", "read_csv_source"): (
        "scan route: its own entry, not a run route"
    ),
    ("profiling/scan_set.py", "_text_frames", "read_csv_source"): (
        "scan route: its own entry, not a run route"
    ),
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _calls(node: ast.AST) -> list[tuple[str, int]]:
    return [
        (name, call.lineno)
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and (name := _call_name(call)) is not None
    ]


def violations(func: ast.FunctionDef, guarded: frozenset[str]) -> list[str]:
    """Guarded calls that are not preceded by the gate, or all of them when there is no gate."""
    calls = _calls(func)
    gate_lines = [line for name, line in calls if name == GATE]
    first = min(gate_lines) if gate_lines else None
    return [
        f"{name}@{line}"
        for name, line in calls
        if name in guarded and (first is None or line < first)
    ]


def _route_violations(tree: ast.AST, func_name: str, guarded: frozenset[str]) -> list[str]:
    return violations(_function(tree, func_name), guarded)


def _parse(rel: str) -> ast.Module:
    return ast.parse((SRC / rel).read_text(encoding="utf-8"))


def _contains_gate(stmt: ast.stmt) -> bool:
    return any(name == GATE for name, _ in _calls(stmt))


def _reparse(tree: ast.Module) -> ast.Module:
    return ast.parse(ast.unparse(tree))


def test_the_gate_precedes_every_reader_on_each_route() -> None:
    for (rel, func_name), guarded in ROUTES.items():
        tree = _parse(rel)
        assert _route_violations(tree, func_name, guarded) == [], (rel, func_name)
        assert any(name == GATE for name, _ in _calls(_function(tree, func_name))), (
            f"{func_name} has no {GATE} call"
        )


def test_moving_the_gate_makes_the_walk_fail() -> None:
    """Seeded mutations: the walk sees a gate moved below the first reader."""
    rel, func_name = "execution/_pipeline_context.py", "build_run_context"
    tree = _parse(rel)
    func = _function(tree, func_name)
    gate_at = next(i for i, s in enumerate(func.body) if _contains_gate(s))
    guarded = ROUTES[(rel, func_name)]
    after = next(
        i
        for i, s in enumerate(func.body)
        if i > gate_at and any(n in guarded for n, _ in _calls(s))
    )
    func.body[gate_at], func.body[after] = func.body[after], func.body[gate_at]
    mutated = _reparse(tree)
    assert _route_violations(mutated, func_name, guarded) != []

    rel, func_name = "execution/_isolated_run.py", "run_pipeline_isolated"
    tree = _parse(rel)
    func = _function(tree, func_name)
    gate_at = next(i for i, s in enumerate(func.body) if _contains_gate(s))
    func.body.append(func.body.pop(gate_at))
    mutated = _reparse(tree)
    assert _route_violations(mutated, func_name, ROUTES[(rel, func_name)]) != []


def _enclosing_calls(tree: ast.Module) -> set[tuple[str, str]]:
    """(enclosing function, reader) for every reader call in a module."""
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for name, _ in _calls(node):
                if name in READER_NAMES:
                    found.add((node.name, name))
    return found


def _source_readers() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(SRC).as_posix()
        found.update((rel, func, reader) for func, reader in _enclosing_calls(tree))
    return found


def test_every_source_reader_call_is_listed_with_a_reason() -> None:
    found = _source_readers()
    unlisted = sorted(found - set(READERS))
    assert unlisted == [], f"a new source reader is not listed in READERS: {unlisted}"
    stale = sorted(set(READERS) - found)
    assert stale == [], f"READERS lists a call that no longer exists: {stale}"
    assert all(READERS.values())
