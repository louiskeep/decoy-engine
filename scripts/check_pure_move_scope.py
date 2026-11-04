"""Scope analysis for the pure-move checker, and its rules 7 and 8 (plan 1.3).

Rule 7 classifies names with the standard library ``symtable`` module, the compiler's own
scope analysis, so a moved definition's globals are what CPython sees, nested functions,
classes, lambdas and comprehensions included. The AST helpers here report the names a
module-level statement binds and reads; ``check_pure_move`` uses them for rule 1c.
"""

from __future__ import annotations

import ast
import builtins
import symtable

BUILTINS = frozenset(dir(builtins))
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
IMPORTS = (ast.Import, ast.ImportFrom)
COMPS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp, ast.Lambda)
_LOCAL = (ast.Store, ast.arg)
_SIMPLE = (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, *DEFS)
_BINDING = (*IMPORTS, *DEFS, ast.ClassDef, ast.Assign, ast.AnnAssign)
_DYNAMIC = {"exec", "eval", "globals"}


def shallow(node: ast.AST):
    for child in ast.iter_child_nodes(node):
        yield child
        yield from () if isinstance(child, (*DEFS, ast.ClassDef, *COMPS)) else shallow(child)


def binds(stmt: ast.stmt, imports: bool = True) -> set[str]:
    """Module-level names ``stmt`` binds; a bare annotation binds nothing."""
    if isinstance(stmt, IMPORTS):
        return {(a.asname or a.name).split(".")[0] for a in stmt.names if imports and a.name != "*"}
    if isinstance(stmt, (*DEFS, ast.ClassDef)):
        return {stmt.name}
    out: set[str] = set()
    for node in () if isinstance(stmt, ast.AnnAssign) and not stmt.value else shallow(stmt):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, (*DEFS, ast.ClassDef, *IMPORTS)):
            out |= binds(node, imports)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
    return out


def def_time(stmt: ast.stmt, lazy: bool) -> list[ast.expr]:
    """Expressions the enclosing scope evaluates when ``stmt`` (a def or class) runs."""
    if isinstance(stmt, ast.ClassDef):
        return [*stmt.decorator_list, *stmt.bases, *(k.value for k in stmt.keywords)]
    a = stmt.args
    out = [*stmt.decorator_list, *a.defaults, *a.kw_defaults]
    notes = [x.annotation for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg] if x]
    return [e for e in (out if lazy else [*out, *notes, stmt.returns]) if e]


def reads(node: ast.AST, lazy: bool) -> set[str]:
    """Names ``node`` reads when it runs at module level; a nested ``def`` body runs later."""
    if isinstance(node, DEFS):
        parts = def_time(node, lazy)
    elif isinstance(node, ast.ClassDef):
        parts = [*def_time(node, lazy), *node.body]
    else:
        lazy_note = lazy and isinstance(node, ast.AnnAssign)
        parts = [node.target, node.value] if lazy_note else list(ast.iter_child_nodes(node))
    out = {node.id} if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) else set()
    out = out.union(*(reads(part, lazy) for part in parts if part))
    if isinstance(node, COMPS):
        local = [n for n in ast.walk(node) if isinstance(getattr(n, "ctx", n), _LOCAL)]
        out -= {getattr(n, "id", None) or getattr(n, "arg", "") for n in local}
    elif isinstance(node, ast.stmt) and not isinstance(node, _SIMPLE):
        out -= {n for s in shallow(node) if isinstance(s, ast.stmt) for n in binds(s)}
    return out


def _outer_names(node: ast.AST):
    """Names an expression reads in its own scope; lambda and comprehension bodies excluded."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        yield node.id
    elif isinstance(node, COMPS):
        a = node.args if isinstance(node, ast.Lambda) else None
        kids = [*a.defaults, *a.kw_defaults] if a else [node.generators[0].iter]
        yield from (n for kid in kids if kid for n in _outer_names(kid))
    else:
        yield from (n for kid in ast.iter_child_nodes(node) for n in _outer_names(kid))


def _tables(table: symtable.SymbolTable):
    yield table
    for child in table.get_children():
        yield from _tables(child)


def _future(tree: ast.Module) -> tuple[bool, bool]:
    keys = [(s.module, a.name) for s in tree.body if isinstance(s, ast.ImportFrom) for a in s.names]
    return ("__future__", "annotations") in keys, any(name == "*" for _, name in keys)


def _annotated(node: ast.AST) -> bool:
    return any(
        getattr(n, "annotation", None) or getattr(n, "returns", None) for n in ast.walk(node)
    )


def _moved(node: ast.stmt, path: str, top, bound: set[str], old_binds: set[str], lazy: bool):
    """Rule 7 for one moved def or class, from its own and its sibling symbol tables."""
    where = f"7: {path}:{node.lineno}: {type(node).__name__} {node.name}"
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    near = [c for c in top.get_children() if start <= c.get_lineno() <= node.end_lineno]
    syms = [s for c in near for t in _tables(c) for s in t.get_symbols()]
    need = {s.get_name() for s in syms if s.is_global()}
    need |= {n for e in def_time(node, lazy) for n in _outer_names(e)}
    writes = {s.get_name() for s in syms if s.is_declared_global() and s.is_assigned()}
    undefined = need - old_binds - BUILTINS  # never bound at base: ruff F821 owns these
    missing = need - bound - undefined - (BUILTINS - old_binds)
    out = [f"{where}: writes global {n}" for n in sorted(writes)]
    out += [f"{where}: calls {n}" for n in sorted(need & _DYNAMIC)]
    return out + [f"{where}: {n} is not bound unconditionally here" for n in sorted(missing)]


def scope_problems(base: str, heads: dict[str, str | None], owners: dict[str, str]) -> list[str]:
    """Rule 7: a moved def keeps every global it reads; rule 8: it keeps lazy annotations."""
    old_top = symtable.symtable(base, "old", "exec")
    old_binds = {s.get_name() for s in old_top.get_symbols() if s.is_assigned() or s.is_imported()}
    lazy, star = _future(ast.parse(base))
    out = ["7: base holds a star import; globals cannot be resolved"] if star else []
    for path in sorted({p for p in owners.values() if heads.get(p)}):
        tree, top = ast.parse(heads[path]), symtable.symtable(heads[path], path, "exec")
        dest_lazy, dest_star = _future(tree)
        out += [f"7: {path}: holds a star import; globals cannot be resolved"] if dest_star else []
        bound = {n for s in tree.body if isinstance(s, _BINDING) for n in binds(s)}
        for node in (s for s in tree.body if owners.get(getattr(s, "name", None)) == path):
            out += _moved(node, path, top, bound, old_binds, lazy)
            lost = lazy and not dest_lazy and _annotated(node)
            out += (
                [f"8: {path}:{node.lineno}: {node.name}: annotations lose lazy evaluation"]
                if lost
                else []
            )
    return out
