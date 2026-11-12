"""AT-E2c-6: the declaration gate sits before every reader on the run, isolated and profile routes.

`prepare_declared_sources` is the engine's pre-read boundary for config-origin sources. Two
walks guard it:

- (a) in `profile_source`, `build_run_context` and `run_pipeline_isolated`, a
  `prepare_declared_sources(` call comes before every call that reads, profiles, stages or
  spawns on that route, in the same function body;
- (b) every source-side call of `build_declared_source(`, `build_profile_source(` and
  `read_csv_source(` in `src/decoy_engine` is in `READERS` with a reason. A new reader fails
  until someone lists it and says why it is safe.

- (c) the three public chunked entries (E2d) are in `ROUTES` too; in
  `_oracle_preflight_admitted` the first chunk's columns are checked (`checked_stream(`) right
  after `next(`, with no profile, compile or per-chunk wrap between; and no `declared=None`
  appears in `src/`.

Covered shapes: a plain call and an attribute call (`mod.name(...)`). Walks (a) to (c) match the
called name, so a reader imported under another name is not seen. The execution-layer checks
at the end resolve import aliases and re-bindings for `prepare_declared_sources` and
`profile_source`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[3] / "src" / "decoy_engine"
GATE = "prepare_declared_sources"
# Execution routes call the choke point in `execution/_guards.py`, which adds the created-column
# policy and then calls the profile boundary; both names count as the gate.
GATES = frozenset({GATE, "prepare_declared_execution_sources"})

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
    ("execution/_chunked.py", "run_mask_pipeline_chunked"): frozenset({"_oracle_preflight"}),
    ("execution/native/_chunked_entry.py", "run_mask_chunked"): frozenset({"_run_chunked"}),
    ("execution/native/_dispatch.py", "run_native_or_oracle_chunked"): frozenset({"_run_chunked"}),
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
    gate_lines = [line for name, line in calls if name in GATES]
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
    return any(name in GATES for name, _ in _calls(stmt))


def _reparse(tree: ast.Module) -> ast.Module:
    return ast.parse(ast.unparse(tree))


def test_the_gate_precedes_every_reader_on_each_route() -> None:
    for (rel, func_name), guarded in ROUTES.items():
        tree = _parse(rel)
        assert _route_violations(tree, func_name, guarded) == [], (rel, func_name)
        assert any(name in GATES for name, _ in _calls(_function(tree, func_name))), (
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


PREFLIGHT = ("execution/_chunked_oracle.py", "_oracle_preflight_admitted")
BEFORE_CHECK = frozenset({"first_chunk_profile", "compile_plan", "refused_per_chunk", "plan_carry"})


def _preflight_order_violations(func: ast.FunctionDef) -> list[str]:
    """The first-chunk check must follow `next(` and precede every profile, compile and wrap."""
    calls = _calls(func)
    pulled = [line for name, line in calls if name == "next"]
    checked = [line for name, line in calls if name == "checked_stream"]
    if not pulled or not checked:
        return ["next or checked_stream is missing"]
    return [
        f"{name}@{line}" for name, line in calls if name in BEFORE_CHECK and line < min(checked)
    ] + (
        [f"checked_stream@{min(checked)} before next@{min(pulled)}"]
        if min(checked) < min(pulled)
        else []
    )


def test_the_first_chunk_is_checked_before_profile_compile_or_wrap() -> None:
    rel, name = PREFLIGHT
    assert _preflight_order_violations(_function(_parse(rel), name)) == []


def test_moving_the_first_chunk_check_below_the_profile_makes_the_walk_fail() -> None:
    rel, name = PREFLIGHT
    tree = _parse(rel)
    func = _function(tree, name)
    holder = next(
        n
        for n in ast.walk(func)
        if isinstance(n, ast.If)
        and any(c == "checked_stream" for stmt in n.body for c, _ in _calls(stmt))
    )
    at = next(
        i for i, s in enumerate(holder.body) if any(c == "checked_stream" for c, _ in _calls(s))
    )
    moved = holder.body.pop(at)
    func.body.append(moved)
    mutated = _reparse(tree)
    assert _preflight_order_violations(_function(mutated, name)) != []


def test_no_route_passes_declared_none() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if (
                        kw.arg == "declared"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is None
                    ):
                        offenders.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno}")
    assert offenders == []


CHOKE_POINT = ("execution/_guards.py", "prepare_declared_execution_sources")


def _local_names(tree: ast.AST, target: str) -> set[str]:
    """Names `target` is reachable under in a module: itself, import aliases, plain re-bindings."""
    names = {target}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname for a in node.names if a.name == target and a.asname)
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Name)
                and node.value.id in names
            ):
                bound = {t.id for t in node.targets if isinstance(t, ast.Name)} - names
                if bound:
                    names |= bound
                    grew = True
    return names


def _resolved_calls(tree: ast.AST, target: str) -> list[ast.Call]:
    """Calls of `target` by its name, an alias of it, or as an attribute (`mod.target(...)`)."""
    names = _local_names(tree, target)
    return [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Name) and call.func.id in names)
            or (isinstance(call.func, ast.Attribute) and call.func.attr == target)
        )
    ]


def _outside_function(tree: ast.Module, calls: list[ast.Call], name: str | None) -> list[ast.Call]:
    if name is None:
        return calls
    body = _function(tree, name)
    return [c for c in calls if not body.lineno <= c.lineno <= (body.end_lineno or body.lineno)]


def _direct_gate_calls(tree: ast.Module, exempt: str | None = None) -> list[int]:
    """Lines calling the profile boundary directly, outside the exempt function's body."""
    calls = _outside_function(tree, _resolved_calls(tree, GATE), exempt)
    return [c.lineno for c in calls]


def _profile_calls_without_declared(tree: ast.Module) -> list[int]:
    return [
        c.lineno
        for c in _resolved_calls(tree, "profile_source")
        if not any(kw.arg == "declared" for kw in c.keywords)
    ]


def _execution_modules() -> list[tuple[str, ast.Module]]:
    return [
        (path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted((SRC / "execution").rglob("*.py"))
    ]


def test_execution_builds_the_capability_only_through_the_choke_point() -> None:
    """The created-column policy rides on `prepare_declared_execution_sources`; a route that
    calls the profile boundary directly would skip it. Only the choke point's own body may."""
    offenders = [
        f"{rel}:{line}"
        for rel, tree in _execution_modules()
        for line in _direct_gate_calls(tree, CHOKE_POINT[1] if rel == CHOKE_POINT[0] else None)
    ]
    assert offenders == []


def test_every_execution_profile_call_passes_the_prepared_capability() -> None:
    """`profile_source` builds its own policy-free capability when `declared=` is absent."""
    offenders = [
        f"{rel}:{line}"
        for rel, tree in _execution_modules()
        for line in _profile_calls_without_declared(tree)
    ]
    assert offenders == []


def test_the_direct_call_check_sees_aliases_attributes_and_the_choke_point_body() -> None:
    aliased = ast.parse(
        "from decoy_engine.profile import prepare_declared_sources as gate\n"
        "def f(r):\n    return gate(r)\n"
    )
    assert _direct_gate_calls(aliased) == [3]
    rebound = ast.parse(
        "from x import prepare_declared_sources\nalias = prepare_declared_sources\n"
        "def f(r):\n    return alias(r)\n"
    )
    assert _direct_gate_calls(rebound) == [4]
    attr = ast.parse("import m\ndef f(r):\n    return m.prepare_declared_sources(r)\n")
    assert _direct_gate_calls(attr) == [3]
    guards = ast.parse(
        "def prepare_declared_execution_sources(r):\n    return prepare_declared_sources(r)\n"
        "def other(r):\n    return prepare_declared_sources(r)\n"
    )
    assert _direct_gate_calls(guards, "prepare_declared_execution_sources") == [4]


def test_the_profile_call_check_sees_a_missing_declared_keyword() -> None:
    missing = ast.parse("from p import profile_source as ps\ndef f(c):\n    return ps(c, seed=1)\n")
    assert _profile_calls_without_declared(missing) == [3]
    kwargs_only = ast.parse("def f(c, k):\n    return profile_source(c, **k)\n")
    assert _profile_calls_without_declared(kwargs_only) == [2]
    passing = ast.parse("def f(c, d):\n    return profile_source(c, declared=d)\n")
    assert _profile_calls_without_declared(passing) == []


LANE_CORES = (
    ("execution/_chunked_oracle.py", "_mask_pipeline_chunked"),
    ("execution/native/chunked/_core.py", "mask_chunked"),
)


def test_each_route_calls_a_read_it_guards() -> None:
    """A route whose function calls none of its guarded names passes the ordering check vacuously."""
    for (rel, func_name), guarded in ROUTES.items():
        called = {name for name, _ in _calls(_function(_parse(rel), func_name))}
        assert guarded & called, (rel, func_name, sorted(guarded))


def _core_route(key: tuple[str, str]) -> frozenset[str]:
    guarded = ROUTES.get(key)
    assert guarded is not None, f"the lane core {key} is not in ROUTES"
    return guarded


@pytest.mark.parametrize("key", LANE_CORES)
def test_moving_the_read_above_the_gate_in_a_lane_core_makes_the_walk_fail(
    key: tuple[str, str],
) -> None:
    guarded = _core_route(key)
    rel, func_name = key
    tree = _parse(rel)
    func = _function(tree, func_name)
    gate_at = next(i for i, s in enumerate(func.body) if _contains_gate(s))
    read_at = next(i for i, s in enumerate(func.body) if any(n in guarded for n, _ in _calls(s)))
    func.body.insert(gate_at, func.body.pop(read_at))
    assert _route_violations(_reparse(tree), func_name, guarded) != []


@pytest.mark.parametrize("key", LANE_CORES)
def test_dropping_the_gate_from_a_lane_core_makes_the_walk_fail(key: tuple[str, str]) -> None:
    guarded = _core_route(key)
    rel, func_name = key
    tree = _parse(rel)
    func = _function(tree, func_name)
    func.body = [s for s in func.body if not _contains_gate(s)]
    assert _route_violations(_reparse(tree), func_name, guarded) != []
