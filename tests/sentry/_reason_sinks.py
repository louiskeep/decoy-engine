"""Where a reason code can enter: classes and plain functions that accept one.

Split from ``_reason_scan`` (its size cap): discovery of the code-bearing sinks is one concern,
the walk that classifies each argument is another.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from pathlib import Path

CODE_ARG_NAMES = frozenset({"code", "reason_code", "route_reason"})


def discover_families(trees: Iterable[ast.Module]) -> dict[str, tuple[int | None, bool]]:
    """Classes that accept a code: name -> (positional index of the code param, keyword-only).

    A class is code-bearing when its ``__init__`` (or dataclass body) has a ``code`` or
    ``reason_code`` parameter, or it declares a ``code`` class attribute; subclasses inherit.
    """
    own: dict[str, tuple[int | None, bool]] = {}
    bases: dict[str, list[str]] = {}
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases.setdefault(node.name, []).extend(
                b.id
                if isinstance(b, ast.Name)
                else (b.attr if isinstance(b, ast.Attribute) else "")
                for b in node.bases
            )
            pos: int | None = None
            found = False
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    args = item.args
                    plain = [a.arg for a in args.posonlyargs + args.args]
                    kwonly = [a.arg for a in args.kwonlyargs]
                    for nm in CODE_ARG_NAMES:
                        if nm in plain:
                            pos, found = plain.index(nm) - 1, True
                        elif nm in kwonly:
                            pos, found = None, True
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    if item.target.id in CODE_ARG_NAMES:
                        found = True
                elif isinstance(item, ast.Assign):
                    if any(
                        isinstance(t, ast.Name) and t.id in CODE_ARG_NAMES for t in item.targets
                    ):
                        found = True
            if found:
                own[node.name] = (pos, pos is None)
    # Inheritance closure by simple class name.
    changed = True
    while changed:
        changed = False
        for name, parents in bases.items():
            if name in own:
                continue
            for parent in parents:
                if parent in own:
                    own[name] = own[parent]
                    changed = True
                    break
    return own


def discover_function_sinks(
    files: Mapping[Path, ast.Module], src: Path
) -> dict[tuple[str, str], int]:
    """Plain functions with a code parameter: ``(path, name)`` -> positional index of that parameter.

    The key is the function's exact identity (its path under ``src`` and its name), so two
    functions that share a name are two sinks. A forwarding helper (``_refusal(code, ...)``)
    passes its code on, so the helper's own forward is vouched for in the adapter table. That
    vouching holds only if every caller passes a registry symbol, which is what treating the
    helper as a sink checks. Methods are left to the class discovery; a keyword-only parameter
    is already seen as a ``code=`` keyword.
    """
    sinks: dict[tuple[str, str], int] = {}
    for path, tree in files.items():
        rel = path.relative_to(src).as_posix()
        methods = {
            id(item) for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef) for item in cls.body
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or id(node) in methods:
                continue
            plain = [a.arg for a in node.args.posonlyargs + node.args.args]
            for name in CODE_ARG_NAMES:
                if name in plain:
                    sinks[(rel, node.name)] = plain.index(name)
                    break
    return sinks
