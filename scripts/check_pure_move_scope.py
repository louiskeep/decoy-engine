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
_ALLOWED = (*IMPORTS, *DEFS, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr)
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


def sure(stmt: ast.stmt) -> set[str]:
    """Names a module-level statement binds at runtime. Only the kinds ``shape`` allows bind;
    an ``if TYPE_CHECKING:`` block binds nothing, so its names never count as bound."""
    return binds(stmt) if isinstance(stmt, _ALLOWED) else set()


def _type_checking(stmt: ast.stmt) -> bool:
    test = getattr(stmt, "test", None)
    named = isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
    named |= isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    return (
        isinstance(stmt, ast.If)
        and named
        and not stmt.orelse
        and all(isinstance(s, IMPORTS) for s in stmt.body)
    )


def _walrus(node: ast.AST) -> bool:
    """A walrus that runs at module level; def and class bodies bind their own scope."""
    if isinstance(node, ast.NamedExpr):
        return True
    kids = (
        def_time(node, False)
        if isinstance(node, (*DEFS, ast.ClassDef))
        else ast.iter_child_nodes(node)
    )
    return any(_walrus(kid) for kid in kids)


def shape(tree: ast.Module, path: str) -> list[str]:
    """Rule 7 by construction: module-level code is straight-line, so what it binds is certain.

    Compound statements (other than a TYPE_CHECKING import block), loose statements and any
    module-level walrus are refused, so no binding depends on a runtime condition.
    """
    out = []
    for stmt in tree.body:
        kind = type(stmt).__name__
        if not isinstance(stmt, _ALLOWED) and not _type_checking(stmt):
            out.append(f"7: {path}:{stmt.lineno}: module-level {kind} is refused")
        elif _walrus(stmt):
            out.append(f"7: {path}:{stmt.lineno}: module-level walrus is refused")
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


def reached(old: str, nodes: list, got: list) -> set[str]:
    """Names a file's code reaches through the old module path: attributes of any alias of it
    (an alias of an ancestor package included), and every name after ``from old import *``."""
    full = [(a, f"{s}.{a.name}".strip(".")) for s, a in got]
    via = {old}
    for a, f in full:
        if old == f or old.startswith(f + "."):
            via.add((a.asname or a.name) + old[len(f) :])
    out = {n.attr for n in nodes if isinstance(n, ast.Attribute) and ast.unparse(n.value) in via}
    if any(s == old and a.name == "*" for s, a in got):
        out |= {n.id for n in nodes if isinstance(n, ast.Name)}
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


def _rebinds(table: symtable.SymbolTable, sym: symtable.Symbol) -> bool:
    """A class body that reads and rebinds a name (``helper = helper``) reads it from globals."""
    return table.get_type() == "class" and sym.is_referenced() and sym.is_assigned()


def _moved(node, path, top, bound, old_binds, lazy, stays, shared):
    """Rule 7 for one def or class, from its own and its sibling symbol tables."""
    where = f"7: {path}:{node.lineno}: {type(node).__name__} {node.name}"
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    near = [c for c in top.get_children() if start <= c.get_lineno() <= node.end_lineno]
    tabs = [t for c in near for t in _tables(c)]
    need = {s.get_name() for t in tabs for s in t.get_symbols() if s.is_global() or _rebinds(t, s)}
    need |= {n for e in def_time(node, lazy) for n in _outer_names(e)}
    undefined = need - old_binds - BUILTINS  # never bound at base: ruff F821 owns these
    missing = need - bound - undefined - (BUILTINS - old_binds)
    out = [f"{where}: calls {n}" for n in sorted(need & _DYNAMIC)]
    out += [f"{where}: shares global {n}" for n in sorted(need & shared) if not stays]
    return out + [f"{where}: {n} is not bound unconditionally here" for n in sorted(missing)]


def scope_problems(base: str, heads: dict, owners: dict[str, str], kept: str) -> list[str]:
    """Rule 7: a moved or retained def keeps every global it reads; rule 8: lazy annotations.

    Only a straight-line module-level binding counts as bound (see ``shape``).
    """
    old_top = symtable.symtable(base, "old", "exec")
    old_binds = {s.get_name() for s in old_top.get_symbols() if s.is_assigned() or s.is_imported()}
    shared = {
        s.get_name() for t in _tables(old_top) for s in t.get_symbols() if s.is_declared_global()
    }
    old_binds |= shared  # a global declaration binds at module level, as ruff F821 sees it
    base_tree = ast.parse(base)
    lazy, star = _future(base_tree)
    out = ["7: base holds a star import; globals cannot be resolved"] if star else []
    out += shape(base_tree, "base")
    moved = {n for n, p in owners.items() if p != kept}
    out += [f"7: {n} is declared global at base and moves" for n in sorted(shared & moved)]
    for path in sorted({p for p in [*owners.values(), kept] if heads.get(p)}):
        tree, top = ast.parse(heads[path]), symtable.symtable(heads[path], path, "exec")
        dest_lazy, dest_star = _future(tree)
        out += [f"7: {path}: holds a star import; globals cannot be resolved"] if dest_star else []
        bound = set().union(*(sure(s) for s in tree.body))
        out += shape(tree, path)
        defs = [s for s in tree.body if isinstance(s, (*DEFS, ast.ClassDef))]
        for node in (s for s in defs if owners.get(s.name, kept) == path):
            out += _moved(node, path, top, bound, old_binds, lazy, path == kept, shared)
            if lazy and not dest_lazy and _annotated(node):
                out += [f"8: {path}:{node.lineno}: {node.name}: annotations lose lazy eval"]
    return out
