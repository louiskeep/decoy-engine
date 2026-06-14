"""Sentry test: validate/check functions do not mutate their inputs.

engineering-best-practices section 2.1: `validate(config)` is read-only, so a
caller can reproduce a run from the config it logged. section 2.2: code that
mutates has a verb name (normalize / populate / apply_defaults / expand) and is
a separate function. So a function named `validate_*` or `check_*` that writes
into a parameter is the exact anti-pattern this gate catches.

This walks the AST of every `validate*` / `check*` function and flags direct
mutation of a non-self parameter:

  - `param[key] = ...`            (subscript assignment)
  - `param.attr = ...`            (attribute assignment)
  - `param[key] += ...`           (augmented subscript assignment)
  - `del param[key]`              (subscript / attribute delete)
  - `param.<mutator>(...)`        (append/extend/update/pop/sort/clear/... )

**Copy boundary (the part section 2.2 cares about).** A parameter that is ever
rebound in the function (e.g. `config = dict(config)` or `rule = rule.copy()`)
is treated as laundered and is NOT checked: mutating the copy is the correct,
blessed idiom. The check is deliberately conservative here. It catches the
common real bug (a validate that writes straight into its input with no copy)
and accepts a false negative on the rare "mutate the original, then copy it"
ordering, rather than risk flagging legitimate copy-then-mutate code. `self`
and `cls` are never treated as inputs.

If a function genuinely must be named validate/check and mutate an input
(it almost never should: rename it per section 2.2), add it to ALLOWLIST with
a rationale in the same PR.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"
REPO = Path(__file__).parents[2]

# Functions in scope: the read-only-contract names (section 2.1 / 2.2).
_TARGET_NAME = re.compile(r"^(validate|check)([_A-Z]|$)")

# Methods that mutate their receiver in place (dict / list / set).
_MUTATORS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "popitem",
        "clear",
        "update",
        "setdefault",
        "sort",
        "reverse",
        "add",
        "discard",
        "__setitem__",
        "__delitem__",
    }
)

# (path relative to repo, function name) -> rationale. Empty: the rule holds.
ALLOWLIST: dict[tuple[str, str], str] = {}


def _func_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = func.args
    names = {
        arg.arg
        for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)
        if arg.arg not in ("self", "cls")
    }
    if a.vararg and a.vararg.arg not in ("self", "cls"):
        names.add(a.vararg.arg)
    if a.kwarg and a.kwarg.arg not in ("self", "cls"):
        names.add(a.kwarg.arg)
    return names


def _rebound_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names assigned to anywhere in the body (so they may be copies, not the
    original input). Includes plain assignments, for-targets, with-as, walrus,
    and comprehension targets."""
    rebound: set[str] = set()

    def _add_target(t: ast.expr) -> None:
        if isinstance(t, ast.Name):
            rebound.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for el in t.elts:
                _add_target(el)
        elif isinstance(t, ast.Starred):
            _add_target(t.value)

    for node in ast.walk(func):
        if isinstance(node, (ast.Assign,)):
            for tgt in node.targets:
                _add_target(tgt)
        elif isinstance(
            node,
            (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr, ast.comprehension),
        ):
            _add_target(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            _add_target(node.optional_vars)
    return rebound


def _base_name(node: ast.expr) -> str | None:
    """Name at the base of a subscript/attribute chain: param[k].x -> 'param'."""
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _mutations_of(func: ast.FunctionDef | ast.AsyncFunctionDef, live: set[str]) -> list[str]:
    """Return human-readable mutation findings against names in `live`."""
    findings: list[str] = []

    def target_base(t: ast.expr) -> str | None:
        if isinstance(t, (ast.Subscript, ast.Attribute)):
            return _base_name(t)
        return None

    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                base = target_base(tgt)
                if base in live:
                    findings.append(f"{base}[...] = ... (line {node.lineno})")
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            base = target_base(node.target)
            if base in live:
                findings.append(f"{base}[...] mutated (line {node.lineno})")
        elif isinstance(node, ast.Delete):
            for tgt in node.targets:
                base = target_base(tgt)
                if base in live:
                    findings.append(f"del {base}[...] (line {node.lineno})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _MUTATORS and isinstance(node.func.value, ast.Name):
                if node.func.value.id in live:
                    findings.append(
                        f"{node.func.value.id}.{node.func.attr}(...) (line {node.lineno})"
                    )
    return findings


def _violations(py_file: Path) -> list[str]:
    rel = str(py_file.relative_to(REPO))
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _TARGET_NAME.match(node.name):
            continue
        if (rel, node.name) in ALLOWLIST:
            continue
        live = _func_params(node) - _rebound_names(node)
        for m in _mutations_of(node, live):
            out.append(f"{rel}:{node.name} mutates input: {m}")
    return out


@pytest.mark.parametrize(
    "py_file",
    sorted(SRC.rglob("*.py")),
    ids=lambda p: str(p.relative_to(REPO)),
)
def test_validate_check_functions_do_not_mutate_inputs(py_file: Path) -> None:
    violations = _violations(py_file)
    assert not violations, (
        "validate/check functions must not mutate their inputs "
        "(best-practices section 2.1). Copy first (config = dict(config)) and "
        "mutate the copy, or rename to a verb per section 2.2:\n  " + "\n  ".join(violations)
    )


def test_allowlist_paths_exist() -> None:
    for (rel, _func), _why in ALLOWLIST.items():
        assert (REPO / rel).exists(), f"ALLOWLIST lists a nonexistent file: {rel}"


def _findings_for(src: str) -> list[str]:
    tree = ast.parse(src)
    func = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    live = _func_params(func) - _rebound_names(func)
    return _mutations_of(func, live)


def test_catches_direct_subscript_mutation() -> None:
    assert _findings_for("def validate(config):\n    config['x'] = 1\n")


def test_catches_mutator_method() -> None:
    assert _findings_for("def check(rule):\n    rule.update({'a': 1})\n")


def test_catches_del() -> None:
    assert _findings_for("def validate(config):\n    del config['x']\n")


def test_ignores_copy_then_mutate() -> None:
    """The blessed idiom: copy first, mutate the copy. Must NOT be flagged."""
    src = "def validate(config):\n    config = dict(config)\n    config['x'] = 1\n"
    assert _findings_for(src) == []


def test_ignores_self_mutation() -> None:
    src = "def check(self):\n    self.state = 1\n    self._cache.append(1)\n"
    assert _findings_for(src) == []


def test_ignores_local_derived_from_param() -> None:
    src = "def validate(config):\n    out = {}\n    out['x'] = config['y']\n"
    assert _findings_for(src) == []
