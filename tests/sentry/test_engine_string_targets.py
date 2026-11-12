"""String-target sentry: every dotted ``decoy_engine`` string in ``src/`` and ``tests/`` resolves.

Class 9 (behavior-preserving refactor drift): a move that leaves ``patch("<old path>.name")``
behind patches nothing, and ``raising=False`` or a skipped lane hides it (H-1 plan 1.1
item 8, AT-1 "String targets, engine"). Resolution is copied from the platform's
``tests/sentry/test_engine_import_surface.py`` (longest importable prefix, then a
``getattr`` chain; the ``pkg.mod:attr`` colon form ``pkgutil.resolve_name`` accepts),
rooted at ``decoy_engine``. Known limit: a target that lands on a re-exported alias still
resolves; plan 1.2 forbids re-exports and the per-slice reviews own that trap.
"""

from __future__ import annotations

import ast
import importlib
import re
import sys
from pathlib import Path

import pytest

from tests.sentry.test_no_residual_removed_surface import FORBIDDEN_TOKENS

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "tests")
ENGINE = "decoy_engine"
_IDENT = r"[A-Za-z_][A-Za-z_0-9]*"
_DOTTED = re.compile(rf"^{ENGINE}(\.{_IDENT})+$")
_COLON = re.compile(rf"^({ENGINE}(?:\.{_IDENT})*):({_IDENT}(?:\.{_IDENT})*)$")
# The removed module the residual-surface sentry lists (that sentry bars its name elsewhere).
_REMOVED = next(token for token in FORBIDDEN_TOKENS if _DOTTED.match(token))
MUTANT = ROOT / "tests" / "fixtures" / "h1" / "engine_string_target_mutant.py.txt"

# Strings that look like engine paths and are not, measured on main. Categories open to
# new entries: logger, filename, deleted (a negative test's list). ``raising-false`` is
# closed at its one pair.
EXEMPT = {
    ("tests/unit/test_structured_events.py", f"{ENGINE}.test"): "logger",
    ("tests/unit/test_public_api.py", f"{ENGINE}.test"): "logger",
    ("tests/sentry/test_ga_stub_exports.py", f"{ENGINE}.schema"): "deleted",
    ("tests/sentry/test_ga_stub_exports.py", f"{ENGINE}.license"): "deleted",
    ("tests/sentry/test_no_residual_removed_surface.py", _REMOVED): "deleted",
    ("tests/unit/test_v2_fixed_width_source.py", f"{ENGINE}.profile._fixed_width_reader.open"): (
        "raising-false"
    ),
}

_module_cache: dict[str, str | None] = {}


def _import_error(module: str) -> str | None:
    """None when ``module`` imports; otherwise the exception type and module name."""
    if module not in _module_cache:
        try:
            importlib.import_module(module)
            _module_cache[module] = None
        except ImportError as exc:
            _module_cache[module] = f"{type(exc).__name__} for {exc.name or module}"
    return _module_cache[module]


def _attr_chain(obj: object, module: str, attrs: list[str]) -> str | None:
    for attr in attrs:
        if not hasattr(obj, attr):
            return f"{module} has no attribute path {'.'.join(attrs)}"
        obj = getattr(obj, attr)
    return None


def _resolve_dotted(path: str) -> str | None:
    parts = path.split(".")
    for cut in range(len(parts), 0, -1):
        prefix = ".".join(parts[:cut])
        if _import_error(prefix) is None:
            return _attr_chain(importlib.import_module(prefix), prefix, parts[cut:])
    return f"{parts[0]} does not import"


def _resolve_string(value: str) -> str | None:
    """A reason when ``value`` names an engine path (dotted or colon form) that is gone."""
    if _DOTTED.match(value):
        return _resolve_dotted(value)
    if m := _COLON.match(value):
        module, attrs = m.groups()
        if (why := _import_error(module)) is not None:
            return why
        return _attr_chain(importlib.import_module(module), module, attrs.split("."))
    return None


def _is_skip_call(node: ast.AST, *, module_level: bool) -> bool:
    """``pytest.skip(..., allow_module_level=True)`` or a bare ``pytest.mark.skip(...)``."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    fn = node.func
    if module_level:
        flags = [k for k in node.keywords if k.arg == "allow_module_level"]
        return fn.attr == "skip" and any(getattr(k.value, "value", None) is True for k in flags)
    return fn.attr == "skip" and isinstance(fn.value, ast.Attribute) and fn.value.attr == "mark"


def _walk_ids(stmts: list[ast.stmt]) -> set[int]:
    return {id(n) for stmt in stmts for n in ast.walk(stmt)}


def _skipped(node: ast.AST) -> bool:
    return any(_is_skip_call(d, module_level=False) for d in node.decorator_list)


def _method_bodies(cls: ast.ClassDef) -> set[int]:
    """Method bodies under a skipped class; the class body itself still runs at import."""
    dead: set[int] = set()
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            dead |= _walk_ids(stmt.body)
        elif isinstance(stmt, ast.ClassDef):
            dead |= _method_bodies(stmt)
    return dead


def _dead_nodes(tree: ast.Module) -> set[int]:
    """Nodes that never run: code after an unconditional module skip, and the bodies under
    an unconditional ``mark.skip``. ``skipif`` is never dead."""
    dead: set[int] = set()
    for i, stmt in enumerate(tree.body):
        if isinstance(stmt, ast.Expr) and _is_skip_call(stmt.value, module_level=True):
            dead |= _walk_ids(tree.body[i + 1 :])
            break
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _skipped(node):
            dead |= _walk_ids(node.body)
        elif isinstance(node, ast.ClassDef) and _skipped(node):
            dead |= _method_bodies(node)
    return dead


def scan_source(source: str, filename: str = "<src>") -> list[tuple[int, str, str]]:
    """``(line, string, reason)`` for each engine-shaped string in live code that does not resolve."""
    tree = ast.parse(source, filename=filename)
    dead = _dead_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if (
            id(node) in dead
            or not isinstance(node, ast.Constant)
            or not isinstance(node.value, str)
        ):
            continue
        if (why := _resolve_string(node.value)) is not None:
            found.append((node.lineno, node.value, why))
    return found


def unresolved() -> dict[tuple[str, str], list[str]]:
    """Every unresolved ``(file, string)`` pair under ``src/`` and ``tests/``, with its sites."""
    out: dict[tuple[str, str], list[str]] = {}
    for top in SCAN_DIRS:
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            for line, value, why in scan_source(path.read_text(encoding="utf-8"), rel):
                out.setdefault((rel, value), []).append(f"{rel}:{line}: {value}: {why}")
    return out


def test_every_decoy_engine_string_target_resolves():
    found = unresolved()
    new = [site for key, sites in sorted(found.items()) if key not in EXEMPT for site in sites]
    stale = sorted(
        f"{path}: {value} no longer unresolved"
        for path, value in EXEMPT
        if (path, value) not in found
    )
    assert not new and not stale, "\n".join(new + stale)


def test_sentry_flags_a_missing_engine_name():
    problems = scan_source(MUTANT.read_text(encoding="utf-8"), MUTANT.name)
    values = [value for _, value, _ in problems]
    assert len(values) == 2 and ":" in values[1]
    assert {value.replace(":", ".") for value in values} == {ENGINE + ".plan._checks.no_such"}


@pytest.mark.parametrize(
    "target",
    [
        "decoy_engine.plan._checks_top_code",
        "decoy_engine.transforms.text_mask",
        "decoy_engine.detection.detectors.REGISTERED_DETECTORS",
        "decoy_engine.detection.detectors:REGISTERED_DETECTORS",
    ],
)
def test_sentry_accepts_real_engine_paths(target):
    assert _resolve_string(target) is None


def test_problem_text_carries_no_exception_message(monkeypatch):
    def refuse(name):
        raise ImportError("secret-message /home/user/private", name=name)

    monkeypatch.setattr(sys.modules[__name__], "_module_cache", {})
    monkeypatch.setattr(importlib, "import_module", refuse)
    why = _resolve_string(ENGINE + ".zz_absent.thing")
    assert why is not None
    assert "secret-message" not in why
    assert "private" not in why
