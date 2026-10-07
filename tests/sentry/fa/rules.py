"""The three ways a sink call may pass (plan D9): an adapter, a capability module, a registry entry."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from tests.sentry.fa import sinks
from tests.sentry.fa.scan import CAPABILITY_ANNOTATIONS, FunctionInfo, Site

try:  # pragma: no cover - interpreter dependent
    import tomllib as toml_reader
except ModuleNotFoundError:  # pragma: no cover
    import tomli as toml_reader  # type: ignore[no-redef]

CAPABILITY_METHODS = {
    "library_path",
    "fileno",
    "create_file",
    "open_file",
    "open_binary",
    "open_text",
    "open_dir",
}
# Raw primitives that resolve a name and therefore need a directory descriptor.
NAME_RESOLVING = {
    "os.open",
    "os.mkdir",
    "os.unlink",
    "os.rmdir",
    "os.rename",
    "os.replace",
    "os.link",
    "os.remove",
}
ALLOWED_RAW = NAME_RESOLVING | {"os.scandir"}
DIR_FD_KEYWORDS = {"dir_fd", "src_dir_fd", "dst_dir_fd"}
HELD_FD_SOURCES = {"os.open", "os.dup", "openat2", "_open_parent", "_open_pending"}


@dataclass(frozen=True)
class Violation:
    site: Site
    reason: str

    def __str__(self) -> str:
        return f"{self.site.module}:{self.site.line} {self.site.function}: {self.site.sink}: {self.reason}"


def load_modules_config(path: Path) -> dict[str, list[str]]:
    data = toml_reader.loads(path.read_text(encoding="utf-8"))
    return {k: list(v) for k, v in data.items() if isinstance(v, list)}


def load_registry(path: Path) -> list[dict[str, object]]:
    data = toml_reader.loads(path.read_text(encoding="utf-8"))
    return list(data.get("entry", []))


def registry_problems(entries: list[dict[str, object]]) -> list[str]:
    """An entry names one call site with its construction rule, owner and reason."""
    problems: list[str] = []
    required = ("module", "function", "sink", "ordinal", "rule", "owner", "reason")
    for i, entry in enumerate(entries):
        for key in required:
            value = entry.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                problems.append(f"entry {i} ({entry.get('module')}) is missing `{key}`")
    return problems


# -- derivation of a path-bearing argument -------------------------------------------------


def _single_name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else None


def capability_derived(expr: ast.AST, info: FunctionInfo, depth: int = 0) -> bool:
    """True when `expr` is built only from a capability parameter's descriptor, library path
    or transaction fd, a descriptor-backed file object over one, or literals."""
    if depth > 8:
        return False
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, (str, bytes, int)) and not isinstance(expr.value, bool)
    if isinstance(expr, ast.Name):
        annotation = info.params.get(expr.id)
        if annotation is not None:
            return any(c in annotation for c in CAPABILITY_ANNOTATIONS) or annotation == "int"
        assigned = info.assigned.get(expr.id)
        return assigned is not None and capability_derived(assigned, info, depth + 1)
    if isinstance(expr, ast.Attribute):
        return (
            capability_derived(expr.value, info, depth + 1)
            if isinstance(expr.value, ast.Name)
            else False
        )
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Attribute) and func.attr in CAPABILITY_METHODS:
            return capability_derived(func.value, info, depth + 1)
        if (isinstance(func, ast.Attribute) and func.attr == "fdopen") or (
            isinstance(func, ast.Name) and func.id == "fdopen"
        ):
            return bool(expr.args) and capability_derived(expr.args[0], info, depth + 1)
        if isinstance(func, ast.Attribute) and func.attr == "open_binary":
            return capability_derived(func.value, info, depth + 1)
        return False
    if isinstance(expr, ast.JoinedStr):
        return all(
            capability_derived(v.value, info, depth + 1)
            for v in expr.values
            if isinstance(v, ast.FormattedValue)
        )
    return False


def _path_args(site: Site) -> list[ast.AST]:
    node = site.node
    if site.sink.startswith(("<obj>.", "Path.", "duckdb.connection.")):
        return list(node.args[:1])
    index = sinks.PATH_ARG_INDEX.get(site.sink, 0)
    return [node.args[index]] if len(node.args) > index else []


def check_adapter_site(site: Site) -> Violation | None:
    """Rule 1: an adapter hands a library only a descriptor, a library path or a transaction
    file taken from one of its capability parameters."""
    info = site.scope_info
    assert info is not None
    if site.kind != "sink":
        return Violation(site, f"{site.kind} call in an adapter module")
    if site.sink == "duckdb.connect":
        first = site.node.args[0] if site.node.args else None
        if first is None:
            first = next((kw.value for kw in site.node.keywords if kw.arg == "database"), None)
        if isinstance(first, ast.Constant) and first.value == ":memory:":
            return None
        return Violation(site, "a DuckDB connection to anything but :memory:")
    if not site.node.args and not site.node.keywords:
        # A sink named as a value: the callee is vetted where it is finally called.
        return None
    args = _path_args(site)
    if not args:
        return None
    for arg in args:
        if not capability_derived(arg, info):
            return Violation(site, "path argument is not derived from a capability parameter")
    return None


# -- capability implementation modules ----------------------------------------------------


def _has_dir_fd(node: ast.Call, wrapped: bool) -> bool:
    keys = {kw.arg for kw in node.keywords if kw.arg}
    if not keys & DIR_FD_KEYWORDS:
        return False
    for kw in node.keywords:
        if (
            kw.arg in DIR_FD_KEYWORDS
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is None
        ):
            return False
    return True


def _rooted_at_held(expr: ast.AST) -> bool:
    """An attribute chain (or method call on one) rooted at `self` or a state object."""
    while isinstance(expr, (ast.Attribute, ast.Call, ast.Subscript)):
        expr = expr.func if isinstance(expr, ast.Call) else expr.value
    return isinstance(expr, ast.Name) and expr.id in {"self", "state", "root"}


def _held_fd(expr: ast.AST, info: FunctionInfo, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(expr, ast.Attribute):
        return _rooted_at_held(expr)
    if isinstance(expr, ast.Name):
        if expr.id in info.params:
            return info.qualname.rsplit(".", 1)[-1].startswith("_") and "fd" in expr.id
        assigned = info.assigned.get(expr.id)
        return assigned is not None and _held_fd(assigned, info, depth + 1)
    if isinstance(expr, ast.Call):
        func = expr.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"open", "dup", "openat2", "_confined_open", "_open_parent", "_call"}:
            return True
        return _rooted_at_held(expr)
    return False


def _upper_constant(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Name) and expr.id.isupper()


def _name_traced(expr: ast.AST, info: FunctionInfo, depth: int = 0) -> bool:
    """A name built from a literal, a policy-generated token, held state, or a parameter the
    function validated with the component validator."""
    if depth > 6:
        return False
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, str)
    if isinstance(expr, ast.Attribute):
        if (
            expr.attr == "name"
            and isinstance(expr.value, ast.Name)
            and expr.value.id in info.entry_vars
        ):
            return True
        return _rooted_at_held(expr)
    if isinstance(expr, ast.Subscript):
        return _upper_constant(expr.value)
    if isinstance(expr, ast.Name):
        if expr.id.isupper():
            return True
        if expr.id in info.self_loop_vars or expr.id in info.validated:
            return True
        if expr.id in info.params:
            return expr.id in info.validated or info.qualname.rsplit(".", 1)[-1].startswith("_")
        assigned = info.assigned.get(expr.id)
        return assigned is not None and _name_traced(assigned, info, depth + 1)
    if isinstance(expr, ast.JoinedStr):
        return all(
            _name_traced(v.value, info, depth + 1)
            for v in expr.values
            if isinstance(v, ast.FormattedValue)
        )
    if isinstance(expr, ast.Call):
        func = expr.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "get" and isinstance(func, ast.Attribute) and _upper_constant(func.value):
            return True
        return name in {"token_hex", "token_urlsafe", "uuid4", "hex"}
    if isinstance(expr, ast.BinOp):
        return _name_traced(expr.left, info, depth + 1) and _name_traced(
            expr.right, info, depth + 1
        )
    return False


def check_capability_site(site: Site) -> Violation | None:
    """Rule 2: only raw fd-relative primitives, with held descriptors and validated names."""
    info = site.scope_info
    assert info is not None
    if site.kind == "descriptor":
        args = site.node.args
        if not args or not _held_fd(args[0], info):
            return Violation(site, "a descriptor argument that does not trace to a held fd")
        return None
    if site.kind != "sink":
        return Violation(site, f"{site.kind} call in a capability module")
    sink = site.sink
    node = site.node
    wrapped = isinstance(node.func, ast.Name) and node.func.id == "_call"
    args = node.args[1:] if wrapped else node.args
    if sink not in ALLOWED_RAW:
        return Violation(
            site, "not a raw fd-relative primitive; third-party and path APIs are forbidden here"
        )
    if sink == "os.scandir":
        if not args or not _held_fd(args[0], info):
            return Violation(site, "os.scandir needs a held directory descriptor")
        return None
    if sink in NAME_RESOLVING:
        if not _has_dir_fd(node, wrapped):
            return Violation(site, "a name-resolving primitive without a dir_fd")
        if sink in {"os.rename", "os.replace", "os.link"}:
            for key in ("src_dir_fd", "dst_dir_fd"):
                kw = {k.arg: k.value for k in node.keywords}.get(key)
                if kw is None or not _held_fd(kw, info):
                    return Violation(site, f"{key} does not trace to a held descriptor")
        else:
            kw = {k.arg: k.value for k in node.keywords}.get("dir_fd")
            if kw is None or not _held_fd(kw, info):
                return Violation(site, "dir_fd does not trace to a held descriptor")
        for arg in args[: 2 if sink in {"os.rename", "os.replace", "os.link"} else 1]:
            if not _name_traced(arg, info):
                return Violation(site, "a name that does not trace to the component validator")
        return None
    return None
