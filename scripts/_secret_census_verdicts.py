"""Verdict checks for the secret-material census: each verdict is a claim the AST re-checks.

Split from `secret_material_census.py` (the scanner) to keep both under the module-size goal.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from secret_material_census import (
    SRC,
    VERDICT_TEXT,
    _bindings,
    _call_name,
    _dotted,
    _is_env_read,
    _own_nodes,
    _passes_secret_role,
)

# ---------------------------------------------------------------------------------------------

VERDICTS = set(VERDICT_TEXT)
_NEEDS_REASON = {"no-raise", "not-secret"}
_NOT_SECRET_FORBIDDEN = ("parameter", "closure", "env-read", "secret-read", "box:")


def _body(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    return body


def _wipe_handler(stmt: ast.stmt, name: str) -> bool:
    if not isinstance(stmt, ast.Try) or stmt.orelse or stmt.finalbody:
        return False
    for handler in stmt.handlers:
        if handler.type is None or _dotted(handler.type) != "BaseException":
            continue
        if not handler.body or not isinstance(handler.body[-1], ast.Raise):
            continue
        if handler.body[-1].exc is not None:
            continue
        deleted = {
            t.id
            for d in handler.body
            if isinstance(d, ast.Delete)
            for t in d.targets
            if isinstance(t, ast.Name)
        }
        if name in deleted:
            return True
    return False


def _wiped(fn: ast.FunctionDef | ast.AsyncFunctionDef, name: str, binding_line: int | None) -> bool:
    body = _body(fn)
    if not body or not _wipe_handler(body[-1], name):
        return False
    prelude = body[:-1]
    if binding_line is None:  # a parameter: bound at entry, so nothing may run before the try
        return not prelude
    if not prelude or not isinstance(prelude[-1], (ast.Assign, ast.AnnAssign)):
        return False
    last = prelude[-1]
    targets = last.targets if isinstance(last, ast.Assign) else [last.target]
    names = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return last.lineno <= binding_line <= (last.end_lineno or last.lineno) and name in names


def _escapes(parent: ast.AST, child: str) -> bool:
    """True when the nested function `child` can outlive `parent`'s call: it is returned,
    yielded, bound to another name or stored anywhere other than as a direct call argument."""
    uses: list[tuple[ast.AST, ast.Name]] = []
    for node in _own_nodes(parent):
        if isinstance(node, ast.keyword):
            continue  # judged with its call below
        for sub in ast.iter_child_nodes(node):
            if isinstance(sub, ast.Name) and sub.id == child and isinstance(sub.ctx, ast.Load):
                uses.append((node, sub))
            if isinstance(sub, ast.keyword) and isinstance(sub.value, ast.Name):
                if sub.value.id == child:
                    uses.append((node, sub.value))
    return any(not isinstance(holder, ast.Call) for holder, _name in uses)


class _Checker:
    def __init__(self, root: Path = SRC) -> None:
        base = root.parent
        self.trees = {
            path.relative_to(base).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
            for path in sorted(root.rglob("*.py"))
        }

    def _node(self, file: str, qual: str) -> ast.AST | None:
        node: ast.AST = self.trees[file]
        for part in qual.split("."):
            found = None
            for child in _scope_children(node):
                if getattr(child, "name", None) == part:
                    found = child
                    break
            if found is None:
                return None
            node = found
        return node

    def check(self, row: dict[str, Any]) -> str | None:
        """None when the code bears out the entry's verdict, else why it does not."""
        verdict, name, arrival = row["verdict"], row["name"], row["arrival"]
        if verdict not in VERDICTS:
            return f"verdict {verdict!r} is not one of {sorted(VERDICTS)}"
        if verdict in _NEEDS_REASON and not str(row.get("reason", "")).strip():
            return f"verdict {verdict!r} needs a reason"
        if verdict == "not-secret" and arrival.startswith(_NOT_SECRET_FORBIDDEN):
            return f"a {arrival} entry cannot be not-secret"
        if verdict in _NEEDS_REASON:
            return None  # a reviewed claim the AST cannot decide; the reason is the record
        qual = row["function"]
        if verdict == "hidden":
            cls = self._node(row["file"], qual)
            if not isinstance(cls, ast.ClassDef) or arrival != "field":
                return "hidden applies to class fields only"
            # A raising `__post_init__` runs inside the generated `__init__`, whose frame holds
            # every field as a parameter (`_opaque.validate_after_init` is the construction).
            for stmt in cls.body:
                if isinstance(stmt, ast.FunctionDef) and stmt.name == "__post_init__":
                    has_init = any(
                        isinstance(s, ast.FunctionDef) and s.name == "__init__" for s in cls.body
                    )
                    if not has_init and any(isinstance(n, ast.Raise) for n in ast.walk(stmt)):
                        return "a raising __post_init__ runs in the frame that holds the field"
            if any(isinstance(s, ast.FunctionDef) and s.name == "__repr__" for s in cls.body):
                return None
            for stmt in cls.body:
                if isinstance(stmt, ast.AnnAssign) and getattr(stmt.target, "id", None) == name:
                    value = stmt.value
                    if (
                        isinstance(value, ast.Call)
                        and _call_name(value) == "field"
                        and any(
                            k.arg == "repr"
                            and isinstance(k.value, ast.Constant)
                            and k.value.value is False
                            for k in value.keywords
                        )
                    ):
                        return None
            return "the field is printed by the class repr"
        if verdict == "contained":
            return self._contained(qual.split(".")[-1])
        if verdict == "boxed" and arrival.startswith("box:"):
            return None
        fn = self._node(row["file"], qual.replace(".<lambda>", ""))
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return f"{qual} not found"
        if verdict == "boxed":
            body = _body(fn)
            ok = (
                len(body) >= 2
                and isinstance(body[0], ast.Assign)
                and isinstance(body[0].value, ast.Call)
                and _call_name(body[0].value) == "OpaqueBox"
                and any(
                    isinstance(n, ast.Name) and n.id == name
                    for a in body[0].value.args
                    for n in ast.walk(a)
                )
                and isinstance(body[1], ast.Delete)
                and any(isinstance(t, ast.Name) and t.id == name for t in body[1].targets)
            )
            return None if ok else "not boxed and deleted by the first two statements"
        if verdict == "unbound":
            if arrival not in {"env-read", "secret-read"}:
                return "unbound applies to environment and SECRET reads only"
            for node in _own_nodes(fn):
                for _target, value in _bindings(node):
                    for sub in ast.walk(value):
                        if getattr(sub, "lineno", None) == row["line"] and (
                            _is_env_read(sub) is not None
                            or (isinstance(sub, ast.Call) and _passes_secret_role(sub))
                        ):
                            return "the read is bound to a name"
            return None
        if verdict == "wiped":
            if arrival.startswith("closure"):
                if qual.endswith(".<lambda>"):
                    return "a lambda closure cannot be shown not to escape; box it"
                parent_qual, child = qual.rsplit(".", 1)
                parent = self._node(row["file"], parent_qual)
                if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return "enclosing function not found"
                if _escapes(parent, child):
                    return f"{child} can outlive {parent_qual}"
                bound = _binding_line(parent, name)
                if not _wiped(parent, name, bound):
                    return f"{parent_qual} does not wipe {name}"
                return None
            if arrival in {"env-read", "secret-read"}:
                for node in _body(fn):
                    if isinstance(node, ast.Assign) and node.lineno <= row["line"] <= (
                        node.end_lineno or node.lineno
                    ):
                        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                        if names and all(_wiped(fn, n, node.lineno) for n in names):
                            return None
                return "the read is not bound right before a wiping try"
            line = None if arrival.startswith("parameter") else row["line"]
            return None if _wiped(fn, name, line) else "no wiping try covers the frame"
        return None

    def _contained(self, simple: str) -> str | None:
        closed = 0
        for tree in self.trees.values():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _call_name(node) == "_call_closed":
                    first = node.args[0] if node.args else None
                    if (isinstance(first, ast.Name) and first.id == simple) or (
                        isinstance(first, ast.Attribute) and first.attr == simple
                    ):
                        closed += 1
                    continue
                if isinstance(node, ast.alias) and simple in (node.name, node.asname):
                    return f"{simple} is imported by name"
                if isinstance(node, ast.Constant) and node.value == simple:
                    return f"{simple} is named in a string (an export list?)"
                refs = [
                    n
                    for n in ast.iter_child_nodes(node)
                    if (isinstance(n, ast.Name) and n.id == simple)
                    or (isinstance(n, ast.Attribute) and n.attr == simple)
                ]
                if refs:
                    return f"{simple} is referenced outside `_call_closed(...)`"
        return None if closed else f"{simple} is never run under `_call_closed`"


def _binding_line(fn: ast.AST, name: str) -> int | None:
    args = fn.args  # type: ignore[attr-defined]
    if any(a.arg == name for a in ast.walk(args) if isinstance(a, ast.arg)):
        return None
    for node in _own_nodes(fn):
        for target, _value in _bindings(node):
            if target.id == name:
                return target.lineno
    return -1


def _scope_children(node: ast.AST) -> Iterator[ast.AST]:
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop(0)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield child
        else:
            stack.extend(ast.iter_child_nodes(child))


def check_census(rows: list[dict[str, Any]], root: Path = SRC) -> list[str]:
    checker = _Checker(root)
    out = []
    for row in rows:
        why = checker.check(row)
        if why is not None:
            out.append(f"{row['file']}:{row['line']} {row['function']} {row['name']}: {why}")
    return out
