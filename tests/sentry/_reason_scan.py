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
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "decoy_engine"
PACKAGE = "decoy_engine"
# The registry's own catalog files are where literal code definitions legitimately live.
REGISTRY_PREFIX = "reasons/"

CODE_ARG_NAMES = frozenset({"code", "reason_code"})
# Module- and class-level constants that hold a reason code by naming convention.
CONST_NAME = re.compile(r"(^|_)(CODE|CODES|REASON)(_|$)|^code$|^reason_code$", re.IGNORECASE)
# A wire code is a lowercase snake/dotted identifier. Prose (spaces) is not a code.
CODE_SHAPE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_ENUM_BASES = frozenset({"Enum", "StrEnum", "IntEnum", "enum.Enum", "enum.StrEnum"})
# Files whose every upper-case string constant is a reason code (frozen decision catalogs).
CATALOG_FILES = frozenset({"execution/physical/_reasons.py", "execution/physical/_shadow_diff_codes.py"})
# Enums that hold reason codes. Other enums (DriverId, PIIClass, OrphanPolicy, ...) are closed
# vocabularies, not reasons; the E0 record lists them as exclusions.
REASON_ENUM_NAME = re.compile(r"(Code|Reason)s?$")


@dataclass(frozen=True)
class Site:
    """One place a reason code is created, stored or forwarded."""

    file: str  # posix path relative to src/decoy_engine
    line: int
    function: str  # enclosing function qualname, "" at module level
    kind: str  # kw | positional | class_attr | module_const | enum_member | attr_write | dict_payload
    family: str  # callee / class / constant name
    value_class: str  # literal | fstring | const_ref | forward | registry | none | dynamic
    expr: str
    codes: tuple[str, ...] = ()  # literal strings this site can carry, when resolvable


def source_files() -> Iterator[Path]:
    yield from sorted(p for p in SRC.rglob("*.py"))


def module_name(path: Path) -> str:
    rel = path.relative_to(SRC.parent).with_suffix("")
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

    def __init__(self) -> None:
        self.trees: dict[Path, ast.Module] = {}
        self.consts: dict[tuple[str, str], str] = {}  # (module, name) -> literal
        self.const_exprs: dict[tuple[str, str], ast.expr] = {}
        self.imports: dict[tuple[str, str], tuple[str, str]] = {}  # (module, local) -> (module, name)
        self.reasons_aliases: dict[str, set[str]] = {}  # module -> names bound to the reasons pkg
        for path in source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            self.trees[path] = tree
            self._index(path, tree)

    def _index(self, path: Path, tree: ast.Module) -> None:
        mod = module_name(path)
        is_pkg = path.name == "__init__.py"
        aliases: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt = node.targets[0]
                if isinstance(tgt, ast.Name):
                    self.const_exprs[(mod, tgt.id)] = node.value
                    if _is_str_const(node.value):
                        self.consts[(mod, tgt.id)] = node.value.value  # type: ignore[attr-defined]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
                self.const_exprs[(mod, node.target.id)] = node.value
                if _is_str_const(node.value):
                    self.consts[(mod, node.target.id)] = node.value.value  # type: ignore[attr-defined]
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

    def resolve_literal(self, mod: str, name: str, seen: frozenset[tuple[str, str]] = frozenset()) -> str | None:
        key = (mod, name)
        if key in seen:
            return None
        if key in self.consts:
            return self.consts[key]
        if key in self.imports:
            src_mod, src_name = self.imports[key]
            return self.resolve_literal(src_mod, src_name, seen | {key})
        return None

    def is_registry_chain(self, mod: str, node: ast.AST) -> bool:
        chain = _chain(node)
        if not chain:
            return False
        if chain[0] in self.reasons_aliases.get(mod, set()):
            return len(chain) >= 2
        # ``from decoy_engine.reasons import runtime`` style: first name is itself a namespace module.
        return False


def _classify(index: _Index, mod: str, node: ast.expr | None, params: frozenset[str]) -> tuple[str, tuple[str, ...]]:
    if node is None:
        return "none", ()
    if isinstance(node, ast.Constant):
        if node.value is None:
            return "none", ()
        if isinstance(node.value, str):
            return "literal", (node.value,)
        return "dynamic", ()
    if isinstance(node, ast.JoinedStr):
        return "fstring", ()
    if index.is_registry_chain(mod, node):
        return "registry", ()
    if isinstance(node, ast.Name):
        lit = index.resolve_literal(mod, node.id)
        if lit is not None:
            return "const_ref", (lit,)
        if (mod, node.id) in index.const_exprs and index.is_registry_chain(mod, index.const_exprs[(mod, node.id)]):
            return "registry", ()
        if node.id in params:
            return "forward", ()
        return "dynamic", ()
    if isinstance(node, ast.Attribute):
        return "forward", ()
    if isinstance(node, ast.BoolOp):
        # ``estimate.code or 'out_of_core_insufficient_memory'`` style defaults.
        lits = tuple(v.value for v in node.values if _is_str_const(v))  # type: ignore[attr-defined]
        return "dynamic", lits
    return "dynamic", ()


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
                b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else "")
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
                    if any(isinstance(t, ast.Name) and t.id in CODE_ARG_NAMES for t in item.targets):
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


def scan() -> list[Site]:
    index = _Index()
    families = _discover_families(index)
    sites: list[Site] = []
    for path, tree in index.trees.items():
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(REGISTRY_PREFIX):
            continue
        mod = module_name(path)
        _Visitor(index, mod, rel, families, sites).visit(tree)
    sites.sort(key=lambda s: (s.file, s.line, s.kind, s.family))
    return sites


class _Visitor(ast.NodeVisitor):
    def __init__(self, index: _Index, mod: str, rel: str, families: dict[str, tuple[int | None, bool]], out: list[Site]):
        self.index, self.mod, self.rel, self.families, self.out = index, mod, rel, families, out
        self.funcs: list[tuple[str, frozenset[str]]] = []
        self.classes: list[tuple[str, bool]] = []  # (name, is_enum)

    # -- context -----------------------------------------------------------------------------
    def _where(self) -> tuple[str, frozenset[str]]:
        if not self.funcs:
            return "", frozenset()
        return ".".join(n for n, _ in self.funcs), self.funcs[-1][1]

    def _emit(self, node: ast.AST, kind: str, family: str, value: ast.expr | None) -> None:
        func, params = self._where()
        vc, codes = _classify(self.index, self.mod, value, params)
        qual = ".".join([c for c, _ in self.classes] + ([func] if func else []))
        self.out.append(
            Site(self.rel, getattr(node, "lineno", 0), qual, kind, family, vc, _expr(value) if value is not None else "", codes)
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        is_enum = any(
            (isinstance(b, ast.Name) and b.id in _ENUM_BASES) or (isinstance(b, ast.Attribute) and b.attr in _ENUM_BASES)
            for b in node.bases
        )
        self.classes.append((node.name, is_enum))
        for item in node.body:
            tgt_name: str | None = None
            value: ast.expr | None = None
            if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                tgt_name, value = item.targets[0].id, item.value
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                tgt_name, value = item.target.id, item.value
            if tgt_name is None or value is None:
                continue
            code_shaped = _is_str_const(value) and CODE_SHAPE.match(value.value)  # type: ignore[attr-defined]
            reason_enum = is_enum and REASON_ENUM_NAME.search(node.name) is not None
            if node.name == "CODES" or reason_enum:
                if code_shaped or self.index.is_registry_chain(self.mod, value):
                    self._emit(item, "enum_member" if reason_enum else "class_attr", f"{node.name}.{tgt_name}", value)
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

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:
        for tgt in node.targets:
            if isinstance(tgt, ast.Attribute) and tgt.attr in CODE_ARG_NAMES:
                self._emit(node, "attr_write", f"{ast.unparse(tgt)}", node.value)
            elif isinstance(tgt, ast.Name) and not self.classes and not self.funcs:
                if (CONST_NAME.search(tgt.id) or self._catalog_const(tgt.id)) and _is_str_const(node.value) and CODE_SHAPE.match(node.value.value):  # type: ignore[attr-defined]
                    self._emit(node, "module_const", tgt.id, node.value)
                elif CONST_NAME.search(tgt.id) and self.index.is_registry_chain(self.mod, node.value):
                    self._emit(node, "module_const", tgt.id, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
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

    def _catalog_const(self, name: str) -> bool:
        return self.rel in CATALOG_FILES and name.isupper()

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, val in zip(node.keys, node.values, strict=True):
            if _is_str_const(key) and key.value in CODE_ARG_NAMES:  # type: ignore[union-attr]
                self._emit(node, "dict_payload", f"{{{key.value!r}: ...}}", val)  # type: ignore[union-attr]
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else (callee.attr if isinstance(callee, ast.Attribute) else "")
        fam = self.families.get(name)
        for kw in node.keywords:
            if kw.arg in CODE_ARG_NAMES:
                self._emit(node, "kw", _expr(callee), kw.value)
        if fam is not None and fam[0] is not None:
            pos = fam[0]
            if pos < len(node.args) and not isinstance(node.args[pos], ast.Starred):
                self._emit(node, "positional", name, node.args[pos])
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[str] = []

        def walk(node: ast.AST) -> None:
            pushed = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if pushed:
                stack.append(node.name)  # type: ignore[attr-defined]
            if isinstance(node, ast.Attribute) and node.attr in CODE_ARG_NAMES and isinstance(node.ctx, ast.Load):
                out.append(Consumer(rel, node.lineno, ".".join(stack), _expr(node)))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and _is_str_const(node.args[1])
                and node.args[1].value in CODE_ARG_NAMES  # type: ignore[attr-defined]
            ):
                out.append(Consumer(rel, node.lineno, ".".join(stack), _expr(node)))
            for child in ast.iter_child_nodes(node):
                walk(child)
            if pushed:
                stack.pop()

        walk(tree)
    out.sort(key=lambda c: (c.file, c.line))
    return out
