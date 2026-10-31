"""Source scanner behind the F29 reason-code inventory and the registry sentry.

Pure AST: no scanned module is imported (optional modules, native companions and the physical
seam all stay unloaded). It finds every site that creates, stores or forwards a machine-readable
reason code, and classifies the value that site carries.

Pattern source: the AST ratchet in ``test_log_interpolation.py``; the code-bearing-sink discovery
(constructors, class attributes, dataclass fields, payload dicts) follows the same
"enumerate the sinks, then classify each argument" shape.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "decoy_engine"
PACKAGE = "decoy_engine"
# The registry's own catalog files are where literal code definitions legitimately live.
REGISTRY_PREFIX = "reasons/"

CODE_ARG_NAMES = frozenset({"code", "reason_code", "route_reason"})
# Module- and class-level constants that hold a reason code by naming convention.
CONST_NAME = re.compile(r"(^|_)(CODE|CODES|REASON)(_|$)|^code$|^reason_code$", re.IGNORECASE)
# A wire code is a lowercase snake/dotted/namespaced identifier (the registry's wire grammar,
# which allows ``:``). Prose (spaces) is not a code.
CODE_SHAPE = re.compile(r"^[a-z][a-z0-9_.:-]*$")
# A slot that holds a routing decision's reason: a local, a field or an unpacked result.
REASON_NAME = re.compile(r"(route|reroute|full_frame)_reason$", re.IGNORECASE)
# A function that decides something: the last slot of a tuple it returns is a reason. Any
# `decide_*` name, not only the route deciders, so a sink or split decision cannot slip past.
ROUTE_DECIDER = re.compile(r"^decide_", re.IGNORECASE)
_ENUM_BASES = frozenset({"Enum", "StrEnum", "IntEnum", "enum.Enum", "enum.StrEnum"})
# Files whose every upper-case string constant is a reason code (frozen decision catalogs).
CATALOG_FILES = frozenset(
    {"execution/physical/_reasons.py", "execution/physical/_shadow_diff_codes.py"}
)
# Enums that hold reason codes. Other enums (DriverId, PIIClass, OrphanPolicy, ...) are closed
# vocabularies, not reasons; the E0 record lists them as exclusions.
REASON_ENUM_NAME = re.compile(r"(Code|Reason)s?$")


@dataclass(frozen=True)
class Site:
    """One place a reason code is created, stored or forwarded."""

    file: str  # posix path relative to src/decoy_engine
    line: int
    function: str  # enclosing function qualname, "" at module level
    kind: (
        str  # kw | positional | class_attr | module_const | enum_member | attr_write | dict_payload
    )
    family: str  # callee / class / constant name
    value_class: str  # literal | fstring | const_ref | forward | registry | none | dynamic
    expr: str
    codes: tuple[str, ...] = ()  # literal strings this site can carry, when resolvable
    # (line, col, end_line, end_col, code) of each literal in the source, for codemods.
    spans: tuple[tuple[int, int, int, int, str], ...] = field(default=(), compare=False)
    # Namespace of the registry symbol this site carries ("" when not registry-valued).
    registry_ns: str = field(default="", compare=False)
    # The keyword / dict key the value is passed under ("" for non-argument sites).
    arg: str = field(default="", compare=False)


def source_files(src: Path = SRC) -> Iterator[Path]:
    yield from sorted(p for p in src.rglob("*.py"))


def module_name(path: Path, src: Path = SRC) -> str:
    rel = path.relative_to(src.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_str_const(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _chain(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


class _Index:
    """Project-wide constant and import tables, built once per scan."""

    def __init__(self, src: Path = SRC) -> None:
        self.src = src
        self.trees: dict[Path, ast.Module] = {}
        self.consts: dict[tuple[str, str], str] = {}  # (module, name) -> literal
        self.const_exprs: dict[tuple[str, str], ast.expr] = {}
        self.imports: dict[
            tuple[str, str], tuple[str, str]
        ] = {}  # (module, local) -> (module, name)
        self.class_consts: dict[tuple[str, str, str], ast.expr] = {}
        self.const_defs: dict[tuple[str, str], tuple[Path, ast.AST]] = {}
        self.reasons_aliases: dict[str, set[str]] = {}  # module -> names bound to the reasons pkg
        for path in source_files(src):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            self.trees[path] = tree
            self._index(path, tree)

    def _index(self, path: Path, tree: ast.Module) -> None:
        mod = module_name(path, self.src)
        is_pkg = path.name == "__init__.py"
        aliases: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt = node.targets[0]
                if isinstance(tgt, ast.Name):
                    self.const_exprs[(mod, tgt.id)] = node.value
                    if _is_str_const(node.value):
                        self.consts[(mod, tgt.id)] = node.value.value  # type: ignore[attr-defined]
                        self.const_defs[(mod, tgt.id)] = (path, node)
            elif (
                isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value
            ):
                self.const_exprs[(mod, node.target.id)] = node.value
                if _is_str_const(node.value):
                    self.consts[(mod, node.target.id)] = node.value.value  # type: ignore[attr-defined]
                    self.const_defs[(mod, node.target.id)] = (path, node)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, ast.Assign)
                        and len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Name)
                    ):
                        self.class_consts[(mod, node.name, item.targets[0].id)] = item.value
                    elif (
                        isinstance(item, ast.AnnAssign)
                        and isinstance(item.target, ast.Name)
                        and item.value is not None
                    ):
                        self.class_consts[(mod, node.name, item.target.id)] = item.value
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = self._resolve_from(mod, is_pkg, node)
                for alias in node.names:
                    local = alias.asname or alias.name
                    self.imports[(mod, local)] = (base, alias.name)
                    if base == f"{PACKAGE}.reasons" or base.startswith(f"{PACKAGE}.reasons."):
                        aliases.add(local)
                    if base == PACKAGE and alias.name == "reasons":
                        aliases.add(local)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == f"{PACKAGE}.reasons":
                        aliases.add(alias.asname or alias.name.split(".")[0])
        self.reasons_aliases[mod] = aliases

    @staticmethod
    def _resolve_from(mod: str, is_pkg: bool, node: ast.ImportFrom) -> str:
        if node.level == 0:
            return node.module or ""
        parts = mod.split(".")
        if not is_pkg:
            parts = parts[:-1]
        if node.level > 1:
            parts = parts[: len(parts) - (node.level - 1)]
        base = ".".join(parts)
        return f"{base}.{node.module}" if node.module else base

    def resolve_def(
        self, mod: str, name: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> tuple[str, str] | None:
        """The ``(module, name)`` whose assignment defines the string constant ``name`` refers to."""
        key = (mod, name)
        if key in seen:
            return None
        if key in self.consts:
            return key
        if key in self.imports:
            src_mod, src_name = self.imports[key]
            return self.resolve_def(src_mod, src_name, seen | {key})
        return None

    def resolve_literal(
        self, mod: str, name: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> str | None:
        key = (mod, name)
        if key in seen:
            return None
        if key in self.consts:
            return self.consts[key]
        if key in self.imports:
            src_mod, src_name = self.imports[key]
            return self.resolve_literal(src_mod, src_name, seen | {key})
        return None

    def class_const(self, mod: str, cls: str, attr: str) -> tuple[str, str]:
        """Classify ``Cls.attr`` when ``Cls`` is a project class with a code-valued attribute."""
        origin = (mod, cls)
        if origin in self.imports:
            origin = self.imports[origin]
        expr = self.class_consts.get((origin[0], origin[1], attr))
        if expr is None:
            return "", ""
        if _is_str_const(expr):
            return "literal", expr.value  # type: ignore[attr-defined]
        if self.is_registry_chain(origin[0], expr):
            return "registry", ""
        return "", ""

    def resolves_to_registry(
        self, mod: str, name: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> bool:
        """Whether a module-level ``name`` is (or is imported as) a registry symbol."""
        key = (mod, name)
        if key in seen:
            return False
        if key in self.const_exprs:
            return self.is_registry_chain(mod, self.const_exprs[key])
        if key in self.imports:
            src_mod, src_name = self.imports[key]
            if src_mod == f"{PACKAGE}.reasons" or src_mod.startswith(f"{PACKAGE}.reasons."):
                # ``from decoy_engine.reasons.runtime import X`` binds a symbol; a bare namespace
                # module (``from decoy_engine.reasons import runtime``) is not itself a code.
                return src_mod != f"{PACKAGE}.reasons"
            return self.resolves_to_registry(src_mod, src_name, seen | {key})
        return False

    def registry_namespace(self, mod: str, node: ast.AST | None, depth: int = 0) -> str | None:
        """The ``decoy_engine.reasons`` namespace a registry-valued expression names."""
        if node is None or depth > 6:
            return None
        chain = _chain(node)
        if chain and self.is_registry_chain(mod, node):
            aliases = self.reasons_aliases.get(mod, set())
            origin = self.imports.get((mod, chain[0]))
            if origin and origin[0] == f"{PACKAGE}.reasons" and origin[1] != "reasons":
                return origin[1]  # ``from decoy_engine.reasons import runtime as R``
            return chain[1] if chain[0] in aliases and len(chain) >= 3 else None
        if isinstance(node, ast.Name):
            key = (mod, node.id)
            if key in self.const_exprs:
                return self.registry_namespace(mod, self.const_exprs[key], depth + 1)
            if key in self.imports:
                src_mod, src_name = self.imports[key]
                if src_mod.startswith(f"{PACKAGE}.reasons."):
                    return src_mod.split(".")[2]
                return self.registry_namespace(src_mod, ast.Name(id=src_name), depth + 1)
        if isinstance(node, ast.Attribute):
            c = _chain(node)
            if c and len(c) == 2:
                origin = self.imports.get((mod, c[0]), (mod, c[0]))
                expr = self.class_consts.get((origin[0], origin[1], c[1]))
                return self.registry_namespace(origin[0], expr, depth + 1)
        if isinstance(node, ast.Subscript) and self.is_registry_chain(mod, node.value):
            return self.registry_namespace(mod, node.value, depth + 1)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            table = self.const_exprs.get((mod, node.value.id))
            if isinstance(table, ast.Dict):
                found = {self.registry_namespace(mod, v, depth + 1) for v in table.values}
                return next(iter(found)) if len(found) == 1 else "mixed"
        return None

    def is_registry_chain(self, mod: str, node: ast.AST) -> bool:
        chain = _chain(node)
        if not chain:
            return False
        if chain[0] in self.reasons_aliases.get(mod, set()):
            return len(chain) >= 2
        # ``from decoy_engine.reasons import runtime`` style: first name is itself a namespace module.
        return False


def _classify_fstring(index: _Index, mod: str, node: ast.JoinedStr, params: frozenset[str]) -> str:
    """``f"{reasons.ns.IDENTITY}:{detail}"`` is a registry identity plus a forwarded detail
    parameter, not an ad-hoc code; any literal head or computed tail is an ``fstring``."""
    head, *rest = node.values
    if not (
        isinstance(head, ast.FormattedValue)
        and index.is_registry_chain(mod, head.value)
        and head.conversion == -1
    ):
        return "fstring"
    for part in rest:
        if isinstance(part, ast.Constant) and part.value == ":":
            continue
        if (
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Name)
            and part.value.id in params
        ):
            continue
        return "fstring"
    return "forward"


def _classify(
    index: _Index, mod: str, node: ast.expr | None, params: frozenset[str]
) -> tuple[str, tuple[str, ...]]:
    if node is None:
        return "none", ()
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "none", ()
        if isinstance(node.value, str):
            return "literal", (node.value,)
        return "dynamic", ()
    if isinstance(node, ast.JoinedStr):
        return _classify_fstring(index, mod, node, params), ()
    if index.is_registry_chain(mod, node):
        return "registry", ()
    if isinstance(node, ast.Name):
        if index.resolves_to_registry(mod, node.id):
            return "registry", ()
        lit = index.resolve_literal(mod, node.id)
        if lit is not None:
            return "const_ref", (lit,)
        if node.id in params:
            return "forward", ()
        return "dynamic", ()
    if isinstance(node, ast.Attribute):
        chain = _chain(node)
        if chain is not None and len(chain) == 2:
            kind, lit = index.class_const(mod, chain[0], chain[1])
            if kind == "registry":
                return "registry", ()
            if kind == "literal":
                return "const_ref", (lit,)
        return "forward", ()
    if isinstance(node, ast.Subscript) and index.is_registry_chain(mod, node.value):
        return (
            "registry",
            (),
        )  # ``reasons.plan.SOME_TABLE[key]``: a finite family owned by the registry
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        # ``_TABLE[key]`` where ``_TABLE`` is a module-level dict whose values are all registry symbols.
        table = index.const_exprs.get((mod, node.value.id))
        if isinstance(table, ast.Dict) and table.values:
            if all(index.is_registry_chain(mod, v) for v in table.values):
                return "registry", ()
        return "dynamic", ()
    if isinstance(node, ast.BoolOp):
        # ``estimate.code or <fallback>``: a literal operand is an ad-hoc code; otherwise the
        # expression only forwards values that are already registry-backed or parameters.
        parts = [_classify(index, mod, v, params) for v in node.values]
        lits = tuple(c for kind, codes in parts if kind in ("literal", "const_ref") for c in codes)
        if lits:
            return "dynamic", lits
        if all(kind in ("registry", "none", "forward") for kind, _ in parts):
            return "forward", ()
        return "dynamic", ()
    return "dynamic", ()


def _span(node: ast.Constant) -> tuple[int, int, int, int, str]:
    return (
        node.lineno,
        node.col_offset,
        node.end_lineno or node.lineno,
        node.end_col_offset or 0,
        node.value,
    )


def _expr(node: ast.AST) -> str:
    text = ast.unparse(node)
    return text if len(text) <= 100 else text[:97] + "..."


def _discover_families(index: _Index) -> dict[str, tuple[int | None, bool]]:
    """Classes that accept a code: name -> (positional index of the code param, keyword-only).

    A class is code-bearing when its ``__init__`` (or dataclass body) has a ``code`` or
    ``reason_code`` parameter, or it declares a ``code`` class attribute; subclasses inherit.
    """
    own: dict[str, tuple[int | None, bool]] = {}
    bases: dict[str, list[str]] = {}
    for tree in index.trees.values():
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


def _target_names(target: ast.AST) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _target_names(elt)


def _reason_bearing(index: _Index) -> frozenset[str]:
    """Functions whose result carries a route reason: a route decider, or one unpacked into a
    ``*route_reason`` slot.

    ``route, route_reason = decide_execution_route(...)`` makes ``decide_execution_route`` a
    producer, so a literal it returns is a reason even though it is never passed as ``code=``.
    """
    names: set[str] = set()
    for tree in index.trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if ROUTE_DECIDER.search(node.name):
                    names.add(node.name)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if any(REASON_NAME.search(n) for t in node.targets for n in _target_names(t)):
                    callee = node.value.func
                    if isinstance(callee, ast.Name):
                        names.add(callee.id)
                    elif isinstance(callee, ast.Attribute):
                        names.add(callee.attr)
    return frozenset(names)


def _reason_literals(node: ast.AST) -> Iterator[ast.expr]:
    """The literal reason values a returned/assigned expression can carry (tuples, branches)."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str) and CODE_SHAPE.match(node.value):
            yield node
    elif isinstance(node, ast.JoinedStr):
        head = node.values[0] if node.values else None
        if (
            isinstance(head, ast.Constant)
            and isinstance(head.value, str)
            and CODE_SHAPE.match(head.value.rstrip(":"))
        ):
            yield node
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            yield from _reason_literals(elt)
    elif isinstance(node, ast.IfExp):
        yield from _reason_literals(node.body)
        yield from _reason_literals(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _reason_literals(value)


def scan(src: Path = SRC) -> list[Site]:
    index = _Index(src)
    families = _discover_families(index)
    bearing = _reason_bearing(index)
    sites: list[Site] = []
    used: set[tuple[str, str]] = set()
    for path, tree in index.trees.items():
        rel = path.relative_to(src).as_posix()
        if rel.startswith(REGISTRY_PREFIX):
            continue
        mod = module_name(path, src)
        visitor = _Visitor(index, mod, rel, families, sites, bearing)
        visitor.visit(tree)
        used.update(visitor.used_consts)
    # A plain-named constant (``MIXED_DRIVER_UNSUPPORTED = "..."``) is a code once it is passed to a
    # code sink, so its definition is a site too. Dedupe against name-pattern sites.
    seen = {(s.file, s.line, s.family) for s in sites if s.kind == "module_const"}
    for def_mod, name in sorted(used):
        path, node = index.const_defs[(def_mod, name)]
        rel = path.relative_to(src).as_posix()
        if rel.startswith(REGISTRY_PREFIX) or (rel, node.lineno, name) in seen:  # type: ignore[attr-defined]
            continue
        value = node.value  # type: ignore[attr-defined]
        sites.append(
            Site(
                rel,
                node.lineno,
                "",
                "module_const",
                name,
                "literal",
                _expr(value),
                (value.value,),
                (_span(value),),
            )  # type: ignore[attr-defined]
        )
    sites.sort(key=lambda s: (s.file, s.line, s.kind, s.family))
    return sites


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        index: _Index,
        mod: str,
        rel: str,
        families: dict[str, tuple[int | None, bool]],
        out: list[Site],
        bearing: frozenset[str] = frozenset(),
    ):
        self.index, self.mod, self.rel, self.families, self.out = index, mod, rel, families, out
        self.bearing = bearing
        self.used_consts: set[tuple[str, str]] = set()
        self.funcs: list[tuple[str, frozenset[str]]] = []
        self.classes: list[tuple[str, bool]] = []  # (name, is_enum)

    # -- context -----------------------------------------------------------------------------
    def _where(self) -> tuple[str, frozenset[str]]:
        if not self.funcs:
            return "", frozenset()
        return ".".join(n for n, _ in self.funcs), self.funcs[-1][1]

    def _emit(
        self, node: ast.AST, kind: str, family: str, value: ast.expr | None, arg: str = ""
    ) -> None:
        func, params = self._where()
        vc, codes = _classify(self.index, self.mod, value, params)
        if vc == "const_ref" and isinstance(value, ast.Name):
            ref = self.index.resolve_def(self.mod, value.id)
            if ref is not None:
                self.used_consts.add(ref)
        qual = ".".join([c for c, _ in self.classes] + ([func] if func else []))
        spans: tuple[tuple[int, int, int, int, str], ...] = ()
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            spans = (_span(value),)
        reg_ns = (self.index.registry_namespace(self.mod, value) or "") if vc == "registry" else ""
        self.out.append(
            Site(
                self.rel,
                getattr(node, "lineno", 0),
                qual,
                kind,
                family,
                vc,
                _expr(value) if value is not None else "",
                codes,
                spans,
                reg_ns,
                arg,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        is_enum = any(
            (isinstance(b, ast.Name) and b.id in _ENUM_BASES)
            or (isinstance(b, ast.Attribute) and b.attr in _ENUM_BASES)
            for b in node.bases
        )
        self.classes.append((node.name, is_enum))
        for item in node.body:
            tgt_name: str | None = None
            value: ast.expr | None = None
            if (
                isinstance(item, ast.Assign)
                and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)
            ):
                tgt_name, value = item.targets[0].id, item.value
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                tgt_name, value = item.target.id, item.value
            if tgt_name is None or value is None:
                continue
            code_shaped = _is_str_const(value) and CODE_SHAPE.match(value.value)  # type: ignore[attr-defined]
            reason_enum = is_enum and REASON_ENUM_NAME.search(node.name) is not None
            if node.name == "CODES" or reason_enum:
                if code_shaped or self.index.is_registry_chain(self.mod, value):
                    self._emit(
                        item,
                        "enum_member" if reason_enum else "class_attr",
                        f"{node.name}.{tgt_name}",
                        value,
                    )
            elif CONST_NAME.search(tgt_name) and (code_shaped or not _is_str_const(value)):
                if code_shaped or tgt_name in CODE_ARG_NAMES:
                    self._emit(item, "class_attr", f"{node.name}.{tgt_name}", value)
        self.generic_visit(node)
        self.classes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        a = node.args
        params = frozenset(x.arg for x in a.posonlyargs + a.args + a.kwonlyargs)
        self.funcs.append((node.name, params))
        self.generic_visit(node)
        self.funcs.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    def visit_Return(self, node: ast.Return) -> None:
        # A literal in the result slot of a reason-producing function is a reason code.
        if self.funcs and self.funcs[-1][0] in self.bearing and node.value is not None:
            # ``(route, reason)``: the reason is the last slot; the route name is not a reason.
            slot = node.value
            if isinstance(slot, ast.Tuple) and slot.elts:
                slot = slot.elts[-1]
            for lit in _reason_literals(slot):
                self._emit(node, "return_slot", self.funcs[-1][0], lit)
        self.generic_visit(node)

    def _reason_local(self, node: ast.AST, names: list[str], value: ast.expr | None) -> None:
        if value is None or not self.funcs:
            return
        if not any(REASON_NAME.search(n) for n in names):
            return
        for lit in _reason_literals(value):
            self._emit(node, "reason_local", ", ".join(names), lit)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._reason_local(node, [n for t in node.targets for n in _target_names(t)], node.value)
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                self._container_const(node, tgt.id, node.value)
            if isinstance(tgt, ast.Attribute) and tgt.attr in CODE_ARG_NAMES:
                self._emit(node, "attr_write", f"{ast.unparse(tgt)}", node.value)
            elif isinstance(tgt, ast.Name) and not self.classes and not self.funcs:
                if (
                    (CONST_NAME.search(tgt.id) or self._catalog_const(tgt.id))
                    and _is_str_const(node.value)
                    and CODE_SHAPE.match(node.value.value)
                ) or (
                    CONST_NAME.search(tgt.id) and self.index.is_registry_chain(self.mod, node.value)
                ):  # type: ignore[attr-defined]
                    self._emit(node, "module_const", tgt.id, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._container_const(node, node.target.id, node.value)
        if (
            isinstance(node.target, ast.Name)
            and node.value is not None
            and not self.classes
            and not self.funcs
            and (CONST_NAME.search(node.target.id) or self._catalog_const(node.target.id))
            and _is_str_const(node.value)
            and CODE_SHAPE.match(node.value.value)  # type: ignore[attr-defined]
        ):
            self._emit(node, "module_const", node.target.id, node.value)
        self.generic_visit(node)

    def _container_const(self, node: ast.AST, name: str, value: ast.expr | None) -> None:
        """A code-named table (dict/set/tuple/list/frozenset) holding literal code strings."""
        if value is None or self.classes or self.funcs or not CONST_NAME.search(name):
            return
        if not isinstance(value, (ast.Dict, ast.Set, ast.List, ast.Tuple, ast.Call)):
            return
        lits = tuple(
            sorted(
                {
                    n.value
                    for n in ast.walk(value)
                    if _is_str_const(n) and CODE_SHAPE.match(n.value)  # type: ignore[attr-defined]
                }
            )
        )
        if lits:
            func, _ = self._where()
            self.out.append(
                Site(
                    self.rel,
                    node.lineno,
                    func,
                    "module_const",
                    f"{name}[]",
                    "literal",
                    _expr(value),
                    lits,
                    tuple(
                        _span(n)
                        for n in ast.walk(value)
                        if isinstance(n, ast.Constant)
                        and isinstance(n.value, str)
                        and CODE_SHAPE.match(n.value)
                    ),
                )
            )

    def _catalog_const(self, name: str) -> bool:
        return self.rel in CATALOG_FILES and name.isupper()

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, val in zip(node.keys, node.values, strict=True):
            if _is_str_const(key) and key.value in CODE_ARG_NAMES:  # type: ignore[union-attr]
                self._emit(node, "dict_payload", f"{{{key.value!r}: ...}}", val, key.value)  # type: ignore[union-attr]
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = node.func
        name = (
            callee.id
            if isinstance(callee, ast.Name)
            else (callee.attr if isinstance(callee, ast.Attribute) else "")
        )
        # ``from x import ExecutionError as EE`` still constructs an ExecutionError.
        origin = self.index.imports.get((self.mod, name))
        fam = self.families.get(origin[1] if origin else name)
        for kw in node.keywords:
            if kw.arg in CODE_ARG_NAMES:
                # ``super().__init__(code=...)`` stores the code for the enclosing class.
                fam_name = (
                    self.classes[-1][0]
                    if self.classes and _expr(callee).startswith("super()")
                    else _expr(callee)
                )
                self._emit(node, "kw", fam_name, kw.value, kw.arg)
        if fam is not None and fam[0] is not None:
            pos = fam[0]
            if pos < len(node.args) and not isinstance(node.args[pos], ast.Starred):
                self._emit(node, "positional", origin[1] if origin else name, node.args[pos])
        self.generic_visit(node)


def to_rows(sites: list[Site]) -> list[dict[str, object]]:
    return [
        {
            "file": s.file,
            "line": s.line,
            "function": s.function,
            "kind": s.kind,
            "family": s.family,
            "value_class": s.value_class,
            "expr": s.expr,
            "codes": list(s.codes),
        }
        for s in sites
    ]


@dataclass(frozen=True)
class Consumer:
    """A place that reads a code off an exception or record."""

    file: str
    line: int
    function: str
    expr: str


def scan_consumers() -> list[Consumer]:
    """Reads of ``.code`` / ``getattr(x, "code")`` outside the registry package."""
    out: list[Consumer] = []
    for path in source_files():
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(REGISTRY_PREFIX):
            continue
        _walk_consumers(rel, ast.parse(path.read_text(encoding="utf-8")), [], out)
    out.sort(key=lambda c: (c.file, c.line))
    return out


def _walk_consumers(rel: str, node: ast.AST, stack: list[str], out: list[Consumer]) -> None:
    pushed = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    if pushed:
        stack.append(node.name)  # type: ignore[attr-defined]
    if (
        isinstance(node, ast.Attribute)
        and node.attr in CODE_ARG_NAMES
        and isinstance(node.ctx, ast.Load)
    ) or (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and _is_str_const(node.args[1])
        and node.args[1].value in CODE_ARG_NAMES  # type: ignore[attr-defined]
    ):
        out.append(Consumer(rel, node.lineno, ".".join(stack), _expr(node)))
    for child in ast.iter_child_nodes(node):
        _walk_consumers(rel, child, stack, out)
    if pushed:
        stack.pop()
