"""One AST index and the scanners for the recurring-defect classes.

``ModuleIndex`` holds the project-wide import-alias, literal-constant and constant-expression
tables. ``tests/sentry/_reason_scan.py`` extends it, so the alias and constant rules exist once.
The class scanners (end anchors, exception context, private facade imports) read it.

Pure AST: no scanned module is imported. Findings carry paths, qualified names and ordinals,
never source text beyond the pattern constant a rule names.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import re._parser as sre_parse  # type: ignore[import-not-found]
else:
    import sre_parse

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "decoy_engine"
PACKAGE = "decoy_engine"


def source_files(src: Path = SRC) -> Iterator[Path]:
    yield from sorted(p for p in src.rglob("*.py"))


def module_name(path: Path, src: Path = SRC) -> str:
    rel = path.relative_to(src.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_str_const(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def attr_chain(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def resolve_from(mod: str, is_pkg: bool, node: ast.ImportFrom) -> str:
    """The absolute module an ``ImportFrom`` names, relative levels included."""
    if node.level == 0:
        return node.module or ""
    parts = mod.split(".")
    if not is_pkg:
        parts = parts[:-1]
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    base = ".".join(parts)
    return f"{base}.{node.module}" if node.module else base


class ModuleIndex:
    """Project-wide constant and import tables, built once per scan."""

    def __init__(self, src: Path = SRC) -> None:
        self.src = src
        self.trees: dict[Path, ast.Module] = {}
        self.consts: dict[tuple[str, str], str] = {}  # (module, name) -> literal
        self.const_exprs: dict[tuple[str, str], ast.expr] = {}
        self.const_defs: dict[tuple[str, str], tuple[Path, ast.AST]] = {}
        self.imports: dict[
            tuple[str, str], tuple[str, str]
        ] = {}  # (module, local) -> (module, name)
        # (module, local) -> dotted module bound by ``import x.y as local`` or ``import x``.
        self.module_aliases: dict[tuple[str, str], str] = {}
        self.module_paths: dict[str, str] = {}  # module -> its path under src
        for path in source_files(src):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            self.trees[path] = tree
            self.module_paths[module_name(path, src)] = path.relative_to(src).as_posix()
            self.index_module(path, tree)

    def index_module(self, path: Path, tree: ast.Module) -> None:
        mod = module_name(path, self.src)
        is_pkg = path.name == "__init__.py"
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt = node.targets[0]
                if isinstance(tgt, ast.Name):
                    self._const(mod, tgt.id, node.value, path, node)
            elif (
                isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value
            ):
                self._const(mod, node.target.id, node.value, path, node)
        for any_node in ast.walk(tree):
            if isinstance(any_node, ast.ImportFrom):
                base = resolve_from(mod, is_pkg, any_node)
                for alias in any_node.names:
                    self.imports[(mod, alias.asname or alias.name)] = (base, alias.name)
            elif isinstance(any_node, ast.Import):
                for alias in any_node.names:
                    if alias.asname:
                        self.module_aliases[(mod, alias.asname)] = alias.name
                    else:
                        top = alias.name.split(".")[0]
                        self.module_aliases[(mod, top)] = top

    def _const(self, mod: str, name: str, value: ast.expr, path: Path, node: ast.AST) -> None:
        self.const_exprs[(mod, name)] = value
        if is_str_const(value):
            self.consts[(mod, name)] = value.value  # type: ignore[attr-defined]
            self.const_defs[(mod, name)] = (path, node)

    @staticmethod
    def _resolve_from(mod: str, is_pkg: bool, node: ast.ImportFrom) -> str:
        return resolve_from(mod, is_pkg, node)

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

    def string_fragments(self, mod: str, node: ast.AST | None, depth: int = 0) -> list[str]:
        """Every constant string piece inside ``node``, names resolved where they are constant."""
        if node is None or depth > 6:
            return []
        if isinstance(node, ast.Name):
            folded = self.fold_string(mod, node)
            return [folded] if folded is not None else []
        if is_str_const(node):
            return [node.value]  # type: ignore[attr-defined]
        out: list[str] = []
        for child in ast.iter_child_nodes(node):
            out.extend(self.string_fragments(mod, child, depth + 1))
        return out

    def fold_string(self, mod: str, node: ast.AST | None, depth: int = 0) -> str | None:
        """The constant string ``node`` evaluates to, or ``None`` when it is not constant."""
        if node is None or depth > 6:
            return None
        if is_str_const(node):
            return node.value  # type: ignore[attr-defined]
        if isinstance(node, ast.Name):
            direct = self.resolve_literal(mod, node.id)
            if direct is not None:
                return direct
            expr = self.const_exprs.get((mod, node.id))
            return self.fold_string(mod, expr, depth + 1) if expr is not None else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.fold_string(mod, node.left, depth + 1)
            right = self.fold_string(mod, node.right, depth + 1)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for part in node.values:
                if isinstance(part, ast.Constant):
                    parts.append(str(part.value))
                elif isinstance(part, ast.FormattedValue) and part.conversion == -1:
                    folded = self.fold_string(mod, part.value, depth + 1)
                    if folded is None or part.format_spec is not None:
                        return None
                    parts.append(folded)
                else:
                    return None
            return "".join(parts)
        return None


# ----- class 1: end anchors in Python ``re`` patterns --------------------------------------

_RE_FUNCS = frozenset(
    {"compile", "match", "search", "fullmatch", "sub", "subn", "split", "findall", "finditer"}
)
# Arguments after which a pattern is not the first positional (all take it first).
_AT_END = sre_parse.AT_END


@dataclass(frozen=True)
class AnchorSite:
    file: str  # posix path relative to the scanned src root's parent
    line: int
    function: str
    pattern: str  # the folded pattern; only compared in meta-tests, never printed in findings

    @property
    def key(self) -> str:
        return f"{self.file}::{self.function or '<module>'}"


def has_end_anchor(pattern: str) -> bool:
    """Whether ``pattern`` holds a ``$`` that stops before a trailing newline (``AT_END``).

    The stdlib parser separates ``$`` (``AT_END``) from ``\\Z`` (``AT_END_STRING``) and from a
    ``$`` inside a character class or an escaped ``\\$``, so none of those are flagged.
    """
    try:
        parsed = sre_parse.parse(pattern)
    except Exception:
        return False
    return _walk_for_end(parsed)


def _walk_for_end(items: object) -> bool:
    for item in items:  # type: ignore[attr-defined]
        op, arg = item
        if op == sre_parse.AT and arg == _AT_END:
            return True
        if _has_end_in(arg):
            return True
    return False


def _has_end_in(arg: object) -> bool:
    if isinstance(arg, sre_parse.SubPattern):
        return _walk_for_end(arg)
    if isinstance(arg, (tuple, list)):
        return any(_has_end_in(a) for a in arg)
    return False


class _ReNames:
    """The local names that mean ``re`` or one of its functions in one module."""

    def __init__(self, index: ModuleIndex, mod: str) -> None:
        self.modules = {
            local
            for (m, local), target in index.module_aliases.items()
            if m == mod and target == "re"
        }
        self.funcs = {
            local: name
            for (m, local), (base, name) in index.imports.items()
            if m == mod and base == "re" and name in _RE_FUNCS
        }

    def function(self, func: ast.expr) -> str | None:
        if isinstance(func, ast.Name):
            return self.funcs.get(func.id)
        chain = attr_chain(func)
        if chain and len(chain) == 2 and chain[0] in self.modules and chain[1] in _RE_FUNCS:
            return chain[1]
        return None


def _fragment_has_end(fragment: str) -> bool:
    if has_end_anchor(fragment):
        return True
    return fragment.endswith("$") and not fragment.endswith("\\$")


def _is_multiline(call: ast.Call) -> bool:
    for sub in [*call.args[1:], *(kw.value for kw in call.keywords)]:
        for node in ast.walk(sub):
            if isinstance(node, ast.Attribute) and node.attr in {"M", "MULTILINE"}:
                return True
            if isinstance(node, ast.Name) and node.id in {"M", "MULTILINE"}:
                return True
    return False


def _enclosing(tree: ast.Module) -> dict[int, str]:
    """Map every node id to the qualified name of its enclosing function or class."""
    owner: dict[int, str] = {}

    def visit(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_qual = qual
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_qual = f"{qual}.{child.name}" if qual else child.name
            owner[id(child)] = child_qual
            visit(child, child_qual)

    visit(tree, "")
    return owner


def end_anchor_sites(index: ModuleIndex) -> tuple[list[AnchorSite], list[AnchorSite]]:
    """``(anchored, unresolved)`` ``re`` call sites; unresolved patterns are not constant."""
    anchored: list[AnchorSite] = []
    unresolved: list[AnchorSite] = []
    for path, tree in index.trees.items():
        mod = module_name(path, index.src)
        rel = path.relative_to(index.src.parent).as_posix()
        names = _ReNames(index, mod)
        owner = _enclosing(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = names.function(node.func)
            if func is None:
                continue
            pattern = index.fold_string(mod, node.args[0])
            where = owner.get(id(node), "")
            if _is_multiline(node):
                continue
            if pattern is not None:
                if has_end_anchor(pattern):
                    anchored.append(AnchorSite(rel, node.lineno, where, pattern))
                continue
            # A pattern assembled from a variable part: judge the constant fragments, so a
            # trailing ``$`` in `rf"...({body})$"` is seen even though `body` is not.
            fragments = index.string_fragments(mod, node.args[0])
            hit = next((f for f in fragments if _fragment_has_end(f)), None)
            if hit is not None:
                anchored.append(AnchorSite(rel, node.lineno, where, hit))
            elif isinstance(node.args[0], ast.Name | ast.Attribute | ast.Call):
                unresolved.append(AnchorSite(rel, node.lineno, where, ""))
    return anchored, unresolved


def python_re_engine_sites(index: ModuleIndex) -> list[tuple[str, int]]:
    """Every ``regex_engine="python-re"`` keyword, which would give Pydantic ``pattern=`` the stdlib ``$``."""
    out: list[tuple[str, int]] = []
    for path, tree in index.trees.items():
        rel = path.relative_to(index.src.parent).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "regex_engine":
                out.append((rel, node.value.lineno))
    return out


# ----- class 3: ``raise ... from None`` inside an ``except`` block -------------------------


def from_none_keys(index: ModuleIndex) -> list[str]:
    """One ``path::qualname::ordinal`` key per ``raise ... from None`` lexically in a handler."""
    keys: list[str] = []
    for path, tree in index.trees.items():
        rel = path.relative_to(index.src.parent).as_posix()
        keys.extend(from_none_tree_keys(tree, rel))
    return sorted(keys)


def from_none_tree_keys(tree: ast.Module, rel: str) -> list[str]:
    owner = _enclosing(tree)
    counts: dict[str, int] = {}
    keys: list[str] = []
    sites = sorted(
        (
            node
            for handler in ast.walk(tree)
            if isinstance(handler, ast.ExceptHandler)
            for node in ast.walk(handler)
            if isinstance(node, ast.Raise)
            and isinstance(node.cause, ast.Constant)
            and node.cause.value is None
        ),
        key=lambda n: (n.lineno, n.col_offset),
    )
    seen: set[int] = set()
    for node in sites:
        if id(node) in seen:  # a nested handler reaches the same raise twice
            continue
        seen.add(id(node))
        qual = owner.get(id(node), "") or "<module>"
        n = counts.get(qual, 0)
        counts[qual] = n + 1
        keys.append(f"{rel}::{qual}::{n}")
    return keys


# ----- class 6: imports of a private module behind a public facade -------------------------

# facade -> src-relative path prefixes that may import its private modules
FACADES: dict[str, tuple[str, ...]] = {
    "decoy_engine._progress_events": ("progress.py", "_progress_events.py"),
    "decoy_engine.reasons._*": ("reasons/",),
}


def _facade_of(target: str) -> str | None:
    """The facade a dotted import target bypasses, or ``None`` when it is public."""
    private = "decoy_engine._progress_events"
    if target == private or target.startswith(private + "."):
        return private
    parts = target.split(".")
    if parts[:2] == ["decoy_engine", "reasons"] and len(parts) > 2 and parts[2].startswith("_"):
        return "decoy_engine.reasons._*"
    return None


def facade_import_findings(index: ModuleIndex) -> list[str]:
    """``path:line imports <private module>`` for each import that bypasses a public facade."""
    out: set[str] = set()
    for path, tree in index.trees.items():
        rel = path.relative_to(index.src).as_posix()
        mod = module_name(path, index.src)
        is_pkg = path.name == "__init__.py"
        for node in ast.walk(tree):
            targets: list[str] = []
            if not isinstance(node, ast.ImportFrom | ast.Import):
                continue
            if isinstance(node, ast.ImportFrom):
                base = resolve_from(mod, is_pkg, node)
                targets = [base, *(f"{base}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            for target in targets:
                facade = _facade_of(target)
                if facade and not rel.startswith(FACADES[facade]):
                    out.add(f"{rel}:{node.lineno} imports {target}")
    return sorted(out)
