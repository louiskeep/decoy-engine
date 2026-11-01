"""A static, within-package call graph: which functions can reach `fileaccess.current()`."""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from tests.sentry.fa.resolve import ProjectIndex, Scope, build_import_map, module_name, resolve

CURRENT_SUFFIXES = ("fileaccess._entry.current", "fileaccess.current")


@dataclass
class FuncDef:
    qualname: str  # module.Class.func
    module: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    kwonly: set[str]
    decorators: list[str]
    calls: set[str] = field(default_factory=set)
    uses_current: bool = False


@dataclass
class Graph:
    funcs: dict[str, FuncDef]
    index: ProjectIndex
    all_exports: dict[str, list[str]]  # module -> names in __all__

    def reachers(self) -> set[str]:
        """Qualified names of every function that can reach a `current()` call."""
        rev: dict[str, set[str]] = defaultdict(set)
        for name, f in self.funcs.items():
            for callee in f.calls:
                rev[callee].add(name)
        seen = {n for n, f in self.funcs.items() if f.uses_current}
        queue = deque(seen)
        while queue:
            cur = queue.popleft()
            for caller in rev.get(cur, ()):
                if caller not in seen:
                    seen.add(caller)
                    queue.append(caller)
        return seen


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    return target.id if isinstance(target, ast.Name) else ""


def build_graph(src_root: Path, package: str = "decoy_engine") -> Graph:
    index = ProjectIndex(package)
    files = sorted((src_root / package).rglob("*.py"))
    trees: dict[Path, ast.Module] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        trees[path] = tree
        index.add(module_name(path, src_root), tree, path.name == "__init__.py")
    funcs: dict[str, FuncDef] = {}
    all_exports: dict[str, list[str]] = {}
    for path, tree in trees.items():
        mod = module_name(path, src_root)
        is_pkg = path.name == "__init__.py"
        scope = Scope(build_import_map(tree.body, mod, is_pkg))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
            ):
                try:
                    all_exports[mod] = list(ast.literal_eval(node.value))
                except (ValueError, SyntaxError):
                    pass
        _collect(tree.body, mod, "", scope, index, funcs, is_pkg)
    return Graph(funcs, index, all_exports)


def _collect(
    body: list[ast.stmt],
    mod: str,
    owner: str,
    scope: Scope,
    index: ProjectIndex,
    funcs: dict[str, FuncDef],
    is_pkg: bool,
) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = f"{mod}.{owner}{node.name}"
            kwonly = {a.arg for a in node.args.kwonlyargs}
            fd = FuncDef(qual, mod, node, kwonly, [_decorator_name(d) for d in node.decorator_list])
            fn_scope = scope.child(build_import_map(node.body, mod, is_pkg))
            cls = owner.rstrip(".")
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    target = resolve(sub.func, fn_scope, index)
                    if target is not None and target != "<dynamic>":
                        if target.endswith(CURRENT_SUFFIXES):
                            fd.uses_current = True
                        fd.calls.add(target)
                        # a class constructor runs its __init__
                        fd.calls.add(f"{target}.__init__")
                    elif (
                        isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "self"
                        and cls
                    ):
                        fd.calls.add(f"{mod}.{cls}.{sub.func.attr}")
                    elif isinstance(sub.func, ast.Name):
                        fd.calls.add(f"{mod}.{sub.func.id}")
            funcs[qual] = fd
            _collect(node.body, mod, f"{owner}{node.name}.", fn_scope, index, funcs, is_pkg)
        elif isinstance(node, ast.ClassDef):
            _collect(node.body, mod, f"{owner}{node.name}.", scope, index, funcs, is_pkg)
