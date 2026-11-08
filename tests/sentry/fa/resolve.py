"""Import, alias and re-export resolution over the stdlib `ast`.

`Resolver` answers one question: what is the canonical dotted name of this call target?
It follows `import ... as`, `from ... import ... as`, re-exports through a package
`__init__`, simple assignment aliases, `functools.partial`, `getattr(obj, "literal")`,
`importlib.import_module("literal")` and `__import__("literal")`. A dynamic form it cannot
resolve to a literal comes back as `DYNAMIC` so a sentry can reject it instead of assuming it
safe.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

DYNAMIC = "<dynamic>"


def module_name(path: Path, src_root: Path) -> str:
    rel = path.relative_to(src_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _absolute(module: str | None, level: int, current: str, is_package: bool) -> str:
    if level == 0:
        return module or ""
    base = current.split(".")
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([*base, module] if module else base)


def build_import_map(body: list[ast.stmt], current: str, is_package: bool) -> dict[str, str]:
    """Names bound by `import` statements directly in `body` (not nested defs)."""
    out: dict[str, str] = {}
    for node in body:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    out[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node.module, node.level, current, is_package)
            for a in node.names:
                if a.name == "*":
                    continue
                out[a.asname or a.name] = f"{base}.{a.name}" if base else a.name
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt):
                    out.update(build_import_map([child], current, is_package))
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    out.update(build_import_map(handler.body, current, is_package))
    return out


@dataclass
class ProjectIndex:
    """Every project module's own bindings, for re-export chasing."""

    package: str
    modules: dict[str, dict[str, str]] = field(default_factory=dict)  # module -> name -> target

    def add(self, name: str, tree: ast.Module, is_package: bool) -> None:
        bound = build_import_map(tree.body, name, is_package)
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    dotted = _dotted(node.value)
                    if dotted is not None:
                        head, _, rest = dotted.partition(".")
                        base = bound.get(head)
                        if base is not None:
                            bound[target.id] = f"{base}.{rest}" if rest else base
        self.modules[name] = bound

    def normalize(self, canonical: str) -> str:
        seen: set[str] = set()
        while canonical not in seen:
            seen.add(canonical)
            parts = canonical.split(".")
            replaced = False
            for cut in range(len(parts) - 1, 0, -1):
                mod = ".".join(parts[:cut])
                if mod in self.modules and parts[cut] in self.modules[mod]:
                    target = self.modules[mod][parts[cut]]
                    nxt = ".".join([target, *parts[cut + 1 :]])
                    if nxt != canonical:
                        canonical = nxt
                        replaced = True
                    break
            if not replaced:
                break
        return canonical


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


@dataclass
class Scope:
    """Names visible in one function (or the module body)."""

    imports: dict[str, str]
    aliases: dict[str, str] = field(default_factory=dict)
    parent: Scope | None = None

    def lookup(self, name: str) -> str | None:
        if name in self.aliases:
            return self.aliases[name]
        if name in self.imports:
            return self.imports[name]
        return self.parent.lookup(name) if self.parent is not None else None

    def child(self, imports: dict[str, str] | None = None) -> Scope:
        return Scope(imports or {}, {}, self)


def resolve(node: ast.AST, scope: Scope, index: ProjectIndex) -> str | None:
    """Canonical dotted name of `node`, `DYNAMIC` for an unresolvable dynamic form, or None."""
    result = _resolve(node, scope, index)
    if result is None or result == DYNAMIC:
        return result
    return index.normalize(result)


def _resolve(node: ast.AST, scope: Scope, index: ProjectIndex) -> str | None:
    if isinstance(node, ast.Name):
        found = scope.lookup(node.id)
        if found is not None:
            return found
        return node.id if node.id in {"open", "__import__", "getattr"} else None
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, scope, index)
        if base is None:
            return None
        if base == DYNAMIC:
            return DYNAMIC
        return f"{base}.{node.attr}"
    if isinstance(node, ast.Call):
        func = _resolve(node.func, scope, index)
        if func is None:
            return None
        func = index.normalize(func) if func != DYNAMIC else func
        if func == "functools.partial" and node.args:
            return _resolve(node.args[0], scope, index)
        if func == "getattr" and len(node.args) >= 2:
            base = _resolve(node.args[0], scope, index)
            attr = node.args[1]
            if base is None:
                return None
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                return f"{base}.{attr.value}"
            return DYNAMIC
        if func in {"importlib.import_module", "__import__"} and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
            return DYNAMIC
    return None
