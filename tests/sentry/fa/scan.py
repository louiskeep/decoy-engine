"""Find every file-system sink call site in a source tree, with its function context.

A `Site` is one call that reaches an Appendix B sink after alias, re-export and path-type
resolution, plus the few non-sink shapes the sentries also police: thread and process
launches outside the propagation helper, `LocalFileAccess` construction, engine-owned handle
construction, DuckDB connection creation, and dynamic dispatch that cannot be resolved.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tests.sentry.fa import sinks
from tests.sentry.fa.callgraph import FuncDef, build_graph
from tests.sentry.fa.resolve import (
    DYNAMIC,
    ProjectIndex,
    Scope,
    build_import_map,
    module_name,
    resolve,
)

PATH_CTORS = {"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.PurePosixPath"}
PATH_PRESERVING = {
    "parent",
    "with_name",
    "with_suffix",
    "with_stem",
    "joinpath",
    "relative_to",
    "absolute",
    "expanduser",
    "resolve",
}
CONNECT_NAMES = {"duckdb.connect"}
CAPABILITY_ANNOTATIONS = ("FileHandle", "DirHandle", "OutputDir", "OutputTxn")


@dataclass(frozen=True)
class Site:
    module: str  # path relative to the source root, posix
    function: str  # qualified name, "<module>" at module level
    sink: str
    kind: str  # sink | process | thread | executor | localfa | engine_handle | dynamic
    line: int
    ordinal: int
    node: ast.Call = field(compare=False, hash=False, repr=False)
    scope_info: FunctionInfo | None = field(default=None, compare=False, hash=False, repr=False)

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.module, self.function, self.sink, self.ordinal)


@dataclass
class FunctionInfo:
    qualname: str
    node: ast.AST
    params: dict[str, str]  # name -> annotation source ("" when unannotated)
    path_vars: set[str] = field(default_factory=set)
    conn_vars: set[str] = field(default_factory=set)
    executor_vars: set[str] = field(default_factory=set)
    entry_vars: set[str] = field(default_factory=set)
    self_loop_vars: set[str] = field(default_factory=set)
    assigned: dict[str, ast.expr] = field(default_factory=dict)
    validated: set[str] = field(default_factory=set)


def _ann(node: ast.arg) -> str:
    return ast.unparse(node.annotation) if node.annotation is not None else ""


def _has_path_annotation(text: str) -> bool:
    return "Path" in text or "PathLike" in text


class ModuleScanner:
    def __init__(self, path: Path, src_root: Path, index: ProjectIndex) -> None:
        self.path = path
        self.rel = path.relative_to(src_root).as_posix()
        self.src_root = src_root
        self.index = index
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.name = module_name(path, src_root)
        self.is_package = path.name == "__init__.py"
        self.sites: list[Site] = []
        self._counts: dict[tuple[str, str], int] = defaultdict(int)
        self.class_path_attrs: dict[str, set[str]] = defaultdict(set)
        self.module_dicts: dict[str, ast.Dict] = {}

    # -- public -------------------------------------------------------------------------
    def scan(self) -> list[Site]:
        module_scope = Scope(build_import_map(self.tree.body, self.name, self.is_package))
        self._collect_module_aliases(module_scope)
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef):
                self._collect_class_paths(node, module_scope)
        info = FunctionInfo("<module>", self.tree, {})
        self._walk_body(self.tree.body, module_scope, info, "")
        return self.sites

    # -- collection ---------------------------------------------------------------------
    def _collect_module_aliases(self, scope: Scope) -> None:
        for node in self.tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    if isinstance(node.value, ast.Dict):
                        self.module_dicts[target.id] = node.value
                    canonical = resolve(node.value, scope, self.index)
                    if canonical is not None and canonical != DYNAMIC:
                        scope.aliases[target.id] = canonical

    def _collect_class_paths(self, cls: ast.ClassDef, scope: Scope) -> None:
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if _has_path_annotation(ast.unparse(stmt.annotation)):
                    self.class_path_attrs[cls.name].add(stmt.target.id)
        for node in ast.walk(cls):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and self._expr_is_path_static(node.value, scope)
                    ):
                        self.class_path_attrs[cls.name].add(target.attr)

    def _expr_is_path_static(self, node: ast.AST, scope: Scope) -> bool:
        if isinstance(node, ast.Call):
            target = resolve(node.func, scope, self.index)
            return target in PATH_CTORS
        return False

    # -- walking ------------------------------------------------------------------------
    def _walk_body(
        self, body: list[ast.stmt], scope: Scope, info: FunctionInfo, owner: str
    ) -> None:
        for stmt in body:
            self._walk_stmt(stmt, scope, info, owner)

    def _walk_stmt(self, stmt: ast.stmt, scope: Scope, info: FunctionInfo, owner: str) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._walk_function(stmt, scope, info, owner)
            return
        if isinstance(stmt, ast.ClassDef):
            for sub in stmt.body:
                self._walk_stmt(sub, scope, info, f"{owner}{stmt.name}.")
            return
        self._learn(stmt, scope, info, owner)
        for _field_name, value in ast.iter_fields(stmt):
            nodes = value if isinstance(value, list) else [value]
            for child in nodes:
                if isinstance(child, ast.stmt):
                    self._walk_stmt(child, scope, info, owner)
                elif isinstance(child, ast.expr):
                    self._walk_expr(child, scope, info)
                elif isinstance(child, ast.excepthandler):
                    self._walk_body(child.body, scope, info, owner)
                elif isinstance(child, ast.withitem):
                    self._walk_expr(child.context_expr, scope, info)
                elif isinstance(child, ast.match_case):
                    self._walk_body(child.body, scope, info, owner)

    def _walk_function(
        self,
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
        parent_scope: Scope,
        parent_info: FunctionInfo,
        owner: str,
    ) -> None:
        qual = f"{owner}{fn.name}"
        scope = parent_scope.child(build_import_map(fn.body, self.name, self.is_package))
        params: dict[str, str] = {}
        for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
            params[arg.arg] = _ann(arg)
        for special in (fn.args.vararg, fn.args.kwarg):
            if special is not None:
                params[special.arg] = _ann(special)
        info = FunctionInfo(qual, fn, params)
        for name, annotation in params.items():
            if _has_path_annotation(annotation):
                info.path_vars.add(name)
        if params.get("self") is not None or params.get("cls") is not None:
            cls_name = owner.rstrip(".").split(".")[-1] if owner else ""
            for attr in self.class_path_attrs.get(cls_name, ()):
                info.path_vars.add(f"self.{attr}")
        for deco in fn.decorator_list:
            self._walk_expr(deco, parent_scope, parent_info)
        self._walk_body(fn.body, scope, info, f"{qual}.")

    def _learn(self, stmt: ast.stmt, scope: Scope, info: FunctionInfo, owner: str) -> None:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            key = self._target_key(target)
            if key is not None:
                info.assigned[key] = stmt.value
                if self._is_path(stmt.value, scope, info):
                    info.path_vars.add(key)
                if self._is_connect(stmt.value, scope):
                    info.conn_vars.add(key)
                if self._is_executor(stmt.value, scope):
                    info.executor_vars.add(key)
                if isinstance(target, ast.Name) and isinstance(stmt.value, ast.Attribute):
                    method = stmt.value.attr
                    recv = stmt.value.value
                    if method in sinks.FILE_METHODS or (
                        method in sinks.PATH_METHODS
                        and (
                            self._is_path(recv, scope, info) or method in sinks.UNIQUE_PATH_METHODS
                        )
                        and resolve(recv, scope, self.index) is None
                    ):
                        scope.aliases[target.id] = f"<bound>.{method}"
                if isinstance(target, ast.Name):
                    canonical = resolve(stmt.value, scope, self.index)
                    if (
                        canonical is not None
                        and canonical != DYNAMIC
                        and not isinstance(stmt.value, ast.Call)
                    ):
                        scope.aliases[target.id] = canonical
                    elif isinstance(stmt.value, ast.Call):
                        partial = resolve(stmt.value, scope, self.index)
                        func = resolve(stmt.value.func, scope, self.index)
                        if partial is not None and func in {
                            "functools.partial",
                            "getattr",
                            "__import__",
                            "importlib.import_module",
                        }:
                            scope.aliases[target.id] = partial
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if _has_path_annotation(ast.unparse(stmt.annotation)):
                info.path_vars.add(stmt.target.id)
            if stmt.value is not None:
                info.assigned[stmt.target.id] = stmt.value
                if self._is_path(stmt.value, scope, info):
                    info.path_vars.add(stmt.target.id)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if isinstance(item.optional_vars, ast.Name):
                    name = item.optional_vars.id
                    info.assigned[name] = item.context_expr
                    if self._is_connect(item.context_expr, scope):
                        info.conn_vars.add(name)
                    if self._is_executor(item.context_expr, scope):
                        info.executor_vars.add(name)
                    if self._is_path(item.context_expr, scope, info):
                        info.path_vars.add(name)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            if isinstance(stmt.target, ast.Name) and self._iter_yields_paths(
                stmt.iter, scope, info
            ):
                info.path_vars.add(stmt.target.id)
            if isinstance(stmt.target, ast.Name):
                text = ast.unparse(stmt.iter)
                if "scandir" in text or ".entries(" in text:
                    info.entry_vars.add(stmt.target.id)
                if (
                    text.startswith("self.")
                    or text.startswith("sorted(self.")
                    or text.startswith("list(self.")
                ):
                    info.self_loop_vars.add(stmt.target.id)
        self._learn_validation(stmt, scope, info)

    def _learn_validation(self, stmt: ast.stmt, scope: Scope, info: FunctionInfo) -> None:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in {
                    "validate_component",
                    "_check",
                    "_validate_table_name",
                    "is_valid_component",
                }:
                    for arg in node.args:
                        if isinstance(arg, ast.Name):
                            info.validated.add(arg.id)

    @staticmethod
    def _target_key(target: ast.expr) -> str | None:
        if isinstance(target, ast.Name):
            return target.id
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            return f"self.{target.attr}"
        return None

    def _iter_yields_paths(self, node: ast.expr, scope: Scope, info: FunctionInfo) -> bool:
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Call):
            return False
        if isinstance(node, ast.Attribute) and node.attr in {"iterdir", "glob", "rglob"}:
            return self._is_path(node.value, scope, info) or node.attr in {"iterdir", "rglob"}
        return False

    # -- typing helpers -----------------------------------------------------------------
    def _is_path(self, node: ast.AST, scope: Scope, info: FunctionInfo) -> bool:
        if isinstance(node, ast.Name):
            return node.id in info.path_vars
        if isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and f"self.{node.attr}" in info.path_vars
            ):
                return True
            if node.attr in PATH_PRESERVING and node.attr != "resolve":
                return self._is_path(node.value, scope, info)
            return False
        if isinstance(node, ast.Subscript):
            return (
                isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"
                and self._is_path(node.value.value, scope, info)
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._is_path(node.left, scope, info) or self._is_path(node.right, scope, info)
        if isinstance(node, ast.IfExp):
            return self._is_path(node.body, scope, info) or self._is_path(node.orelse, scope, info)
        if isinstance(node, ast.BoolOp):
            return any(self._is_path(v, scope, info) for v in node.values)
        if isinstance(node, ast.Call):
            target = resolve(node.func, scope, self.index)
            if target in PATH_CTORS or target in {"pathlib.Path.home", "pathlib.Path.cwd"}:
                return True
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in PATH_PRESERVING:
                return self._is_path(func.value, scope, info)
        return False

    def _is_connect(self, node: ast.AST, scope: Scope) -> bool:
        if not isinstance(node, ast.Call):
            return False
        return is_connect_name(resolve(node.func, scope, self.index) or "")

    def _is_executor(self, node: ast.AST, scope: Scope) -> bool:
        if not isinstance(node, ast.Call):
            return False
        return (resolve(node.func, scope, self.index) or "") in {
            "concurrent.futures.ThreadPoolExecutor",
            "concurrent.futures.thread.ThreadPoolExecutor",
        }

    # -- expressions --------------------------------------------------------------------
    def _walk_expr(self, expr: ast.expr, scope: Scope, info: FunctionInfo) -> None:
        nodes = list(_walk_no_defs(expr))
        call_funcs = {id(n.func) for n in nodes if isinstance(n, ast.Call)}
        # `_call(os.open, ...)`: the wrapper call is the site, not its first argument.
        for n in nodes:
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_call"
                and n.args
            ):
                call_funcs.add(id(n.args[0]))
        # `isinstance(x, SomeHandler)` names a class, it does not use it.
        for n in nodes:
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id
                in {
                    "isinstance",
                    "issubclass",
                }
            ):
                call_funcs.update(id(a) for a in n.args)
        for node in nodes:
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, ast.Call):
                self._classify_call(node, scope, info)
            elif isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in call_funcs:
                # A sink handed over as a value (`guarded(..., pq.read_table, ...)`) is a use.
                target = resolve(node, scope, self.index)
                if (
                    target is not None
                    and target != DYNAMIC
                    and sinks.is_sink_name(target)
                    and not target.rsplit(".", 1)[-1].isupper()
                ):
                    if not _is_inner_of_attribute(node, nodes):
                        self._record_ref(info, target, node)

    def _record(self, info: FunctionInfo, sink: str, kind: str, node: ast.Call) -> None:
        function = info.qualname
        count = self._counts[(function, sink)]
        self._counts[(function, sink)] += 1
        self.sites.append(Site(self.rel, function, sink, kind, node.lineno, count, node, info))

    def _record_ref(self, info: FunctionInfo, sink: str, node: ast.AST) -> None:
        function = info.qualname
        count = self._counts[(function, sink)]
        self._counts[(function, sink)] += 1
        call = ast.Call(func=node, args=[], keywords=[])  # type: ignore[arg-type]
        ast.copy_location(call, node)
        self.sites.append(Site(self.rel, function, sink, "sink", node.lineno, count, call, info))

    def _classify_call(self, node: ast.Call, scope: Scope, info: FunctionInfo) -> None:
        target = resolve(node.func, scope, self.index)
        func = node.func
        # Dynamic dispatch whose target could be a sink.
        if target == DYNAMIC:
            self._record(info, "<dynamic>", "dynamic", node)
            return
        if isinstance(func, ast.Subscript) and isinstance(func.value, ast.Name):
            mapping = self.module_dicts.get(func.value.id)
            if mapping is not None and any(
                (resolve(v, scope, self.index) or "")
                and sinks.is_sink_name(resolve(v, scope, self.index) or "")
                for v in mapping.values
            ):
                self._record(info, f"<mapping {func.value.id}>", "dynamic", node)
                return
        if target is not None:
            self._classify_named(node, target, scope, info)
        if isinstance(func, ast.Attribute):
            self._classify_method(node, func, scope, info)
        # `_call(os.open, ...)`: the wrapper's first argument is the real callee.
        if isinstance(func, ast.Name) and func.id == "_call" and node.args:
            wrapped = resolve(node.args[0], scope, self.index)
            if wrapped is not None and sinks.is_sink_name(wrapped):
                self._record(info, wrapped, "sink", node)

    def _classify_named(
        self, node: ast.Call, target: str, scope: Scope, info: FunctionInfo
    ) -> None:
        if target.startswith("<bound>."):
            method = target.split(".", 1)[1]
            label = f"<obj>.{method}" if method in sinks.FILE_METHODS else f"Path.{method}"
            self._record(info, label, "sink", node)
            return
        if target.endswith("fileaccess._local.LocalFileAccess") or target == (
            "decoy_engine.fileaccess.LocalFileAccess"
        ):
            self._record(info, "LocalFileAccess", "localfa", node)
            return
        if target.endswith(("engine_handle", "engine_dir_handle")) and "fileaccess" in target:
            self._record(info, target.rsplit(".", 1)[-1], "engine_handle", node)
            return
        if target == "threading.Thread" and any(kw.arg == "target" for kw in node.keywords):
            kw = next(k for k in node.keywords if k.arg == "target")
            if not _is_run_in_context(kw.value, scope, self.index):
                self._record(info, "threading.Thread", "thread", node)
            return
        if target in sinks.DESCRIPTOR_ONLY:
            self._record(info, target, "descriptor", node)
            return
        if sinks.is_process_sink(target):
            self._record(info, target, "process", node)
            return
        if target.startswith("tempfile.") and target in sinks.EXACT:
            self._record(info, target, "sink", node)
            return
        if sinks.is_sink_name(target):
            self._record(info, target, "sink", node)

    def _classify_method(
        self, node: ast.Call, func: ast.Attribute, scope: Scope, info: FunctionInfo
    ) -> None:
        attr = func.attr
        recv = func.value
        recv_canonical = resolve(recv, scope, self.index)
        if recv_canonical is not None and recv_canonical != DYNAMIC:
            # A module-level function such as `os.path.exists` was matched by name already, and a
            # project wrapper (an adapter function) is vetted where it is defined.
            if (
                attr in sinks.FILE_METHODS
                and not recv_canonical.startswith(self.index.package + ".")
                and not sinks.is_sink_name(f"{recv_canonical}.{attr}")
            ):
                self._record(info, f"<obj>.{attr}", "sink", node)
            return
        if attr in sinks.FILE_METHODS:
            self._record(info, f"<obj>.{attr}", "sink", node)
            return
        if attr in sinks.PATH_METHODS:
            is_path = self._is_path(recv, scope, info)
            # `os.DirEntry.is_dir(follow_symlinks=...)` shares the name; a Path method has no such keyword.
            dir_entry = any(kw.arg == "follow_symlinks" for kw in node.keywords)
            unique = (
                attr in sinks.UNIQUE_PATH_METHODS
                and not _is_capability(recv, info)
                and not dir_entry
            )
            if is_path or unique:
                self._record(info, f"Path.{attr}", "sink", node)
                return
        if attr in sinks.DUCKDB_CONNECTION_METHODS:
            key = _name_key(recv)
            if key is not None and key in info.conn_vars:
                self._record(info, f"duckdb.connection.{attr}", "sink", node)
                return
        if attr in {"submit", "map"}:
            key = _name_key(recv)
            chained = self._is_executor(recv, scope)  # `ThreadPoolExecutor().submit(...)`
            if (chained or (key is not None and key in info.executor_vars)) and node.args:
                if not _is_run_in_context(node.args[0], scope, self.index):
                    self._record(info, f"executor.{attr}", "executor", node)


def is_connect_name(target: str) -> bool:
    return target in CONNECT_NAMES or target.endswith(
        (".connect_duckdb", ".connect_internal", ".untrusted_session")
    )


def _is_inner_of_attribute(node: ast.AST, nodes: list[ast.AST]) -> bool:
    """True when `node` is the `.value` of a longer attribute chain also in `nodes`."""
    return any(isinstance(n, ast.Attribute) and n.value is node for n in nodes)


def _is_capability(recv: ast.expr, info: FunctionInfo) -> bool:
    if isinstance(recv, ast.Name):
        annotation = info.params.get(recv.id, "")
        return any(c in annotation for c in CAPABILITY_ANNOTATIONS)
    return False


def _name_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return f"self.{node.attr}"
    return None


def _is_run_in_context(node: ast.expr, scope: Scope, index: ProjectIndex) -> bool:
    if isinstance(node, ast.Call):
        target = resolve(node.func, scope, index) or ""
        return target.endswith("run_in_context")
    return False


def _walk_no_defs(node: ast.AST) -> Iterator[ast.AST]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(child)


def scan_tree(src_root: Path, package: str = "decoy_engine") -> tuple[list[Site], ProjectIndex]:
    """Scan every production module under `src_root/package`."""
    index = ProjectIndex(package)
    files = sorted((src_root / package).rglob("*.py"))
    trees: dict[Path, ast.Module] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        trees[path] = tree
        index.add(module_name(path, src_root), tree, path.name == "__init__.py")
    sites: list[Site] = []
    for path in files:
        sites.extend(ModuleScanner(path, src_root, index).scan())
    return sites, index


# -- connection and scratch-reader flow (F25 plan 4.1): wider than the D9 `conn_vars` above, used
# only for `scratch_native` membership, walked over `callgraph.py`'s call edges (no second graph).
_FA = "decoy_engine.fileaccess"
WRAPPERS = frozenset({f"{_FA}.scratch_native", f"{_FA}._scratch.scratch_native"})
ITER_WRAPPERS = frozenset({f"{_FA}.scratch_native_iter", f"{_FA}._scratch.scratch_native_iter"})
_READS = "parquet.read_metadata parquet.read_schema parquet.read_table parquet.ParquetFile"
READ_FORMS = frozenset(f"pyarrow.{n}" for n in f"{_READS} ipc.open_stream ipc.open_file".split())
READ_FORMS |= {"pyarrow.OSFile", "pyarrow.memory_map"}
PLAIN_DATA = frozenset({"fetchone", "fetchall", "fetchmany", "fetchdf", "fetchnumpy", "df", "pl"})
CONN, RESULT, READER = "conn", "result", "reader"


def own_nodes(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    for stmt in fn.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield from _walk_no_defs(stmt)


class NativeFlow:
    """Connections, results, readers via parameters, returns, `self.<attr>` and assignments."""

    def __init__(self, src_root: Path, package: str) -> None:
        graph = build_graph(src_root, package)
        self.index, self.funcs = graph.index, graph.funcs
        self.callees: dict[int, list[str]] = defaultdict(list)
        for fd in self.funcs.values():
            for edge in fd.calls:
                self.callees[id(edge.call)].append(edge.callee)
        self.params: set[tuple[str, str]] = set()  # (function, parameter) handed a connection
        self.returners: set[str] = set()
        self.attrs: dict[tuple[str, str], str] = {}  # (module.Class, attribute) -> kind
        self.names: dict[str, dict[str, str]] = {}
        state = None
        while state != (len(self.params), len(self.returners), len(self.attrs)):
            state = (len(self.params), len(self.returners), len(self.attrs))
            for qual in sorted(self.funcs, key=lambda q: q.count(".")):  # enclosing ones first
                self._analyse(self.funcs[qual])

    def target(self, node: ast.AST, fd: FuncDef) -> str:
        assert fd.scope is not None  # named as the call graph names a callee
        found = resolve(node, fd.scope, self.index)
        if found and found != DYNAMIC:
            return found
        if isinstance(node, ast.Name):
            return f"{fd.module}.{node.id}"
        if isinstance(node, ast.Attribute) and getattr(node.value, "id", "") == "self":
            return f"{fd.qualname.rsplit('.', 1)[0]}.{node.attr}"
        return ""

    def is_wrapper(self, node: ast.AST, fd: FuncDef, names: frozenset[str] = WRAPPERS) -> bool:
        return isinstance(node, ast.Call) and self.target(node.func, fd) in names

    def connects(self, ref: ast.AST, fd: FuncDef) -> bool:
        found = self.target(ref, fd)
        return is_connect_name(found) or found in self.returners

    def kind(self, node: ast.AST, fd: FuncDef, names: dict[str, str]) -> str | None:
        if isinstance(node, ast.Name):
            return names.get(node.id) or (CONN if (fd.qualname, node.id) in self.params else None)
        if isinstance(node, ast.Attribute) and getattr(node.value, "id", "") == "self":
            owner = fd.qualname.rsplit(".", 1)[0]
            return names.get(f"self.{node.attr}") or self.attrs.get((owner, node.attr))
        if not isinstance(node, ast.Call) or self.is_wrapper(node, fd, ITER_WRAPPERS):
            return None
        ref = (node.args or [node.func])[0] if self.is_wrapper(node, fd) else node.func
        if self.connects(ref, fd):
            return CONN
        if self.target(ref, fd) in READ_FORMS:
            return READER
        if isinstance(ref, ast.Call) and self.target(ref.func, fd) == "getattr" and ref.args:
            ref = ast.Attribute(value=ref.args[0], attr="", ctx=ast.Load())
        if isinstance(ref, ast.Attribute) and self.kind(ref.value, fd, names) in (CONN, RESULT):
            return None if ref.attr in PLAIN_DATA else RESULT
        return None

    def _analyse(self, fd: FuncDef) -> None:
        enclosing = self.names.get(fd.qualname.rsplit(".", 1)[0], {})  # a closure's names
        names = self.names.setdefault(fd.qualname, dict(enclosing))
        nodes = list(own_nodes(fd.node))
        size = -1
        while size != len(names):
            size = len(names)
            for node in nodes:
                if isinstance(node, ast.Assign):
                    pairs = [(t, node.value) for t in node.targets]
                elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                    pairs = [(node.target, node.value)]
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    pairs = [(i.optional_vars, i.context_expr) for i in node.items]
                else:
                    continue
                for target, value in pairs:
                    self._bind(target, value, fd, names)
        for node in nodes:
            if isinstance(node, ast.Return) and self.kind(node.value or node, fd, names) == CONN:
                self.returners.add(fd.qualname)
            elif isinstance(node, ast.Call):
                self._bind_arguments(node, fd, names)

    def _bind(self, to: ast.AST | None, value: ast.AST, fd: FuncDef, names: dict[str, str]) -> None:
        found = self.kind(value, fd, names)
        if found is None:
            return
        if isinstance(to, ast.Name):
            names[to.id] = found
        elif isinstance(to, ast.Attribute) and getattr(to.value, "id", "") == "self":
            names[f"self.{to.attr}"] = found
            self.attrs[(fd.qualname.rsplit(".", 1)[0], to.attr)] = found

    def _bind_arguments(self, call: ast.Call, fd: FuncDef, names: dict[str, str]) -> None:
        for target in filter(None, map(self.funcs.get, self.callees.get(id(call), []))):
            args = target.node.args
            positional = [a.arg for a in [*args.posonlyargs, *args.args]]
            bound = isinstance(call.func, ast.Attribute) or target.node.name == "__init__"
            positional = positional[int(bound and positional[:1] in (["self"], ["cls"])) :]
            pairs = [*zip(positional, call.args, strict=False)]
            pairs += [(k.arg, k.value) for k in call.keywords]
            for param, arg in pairs:
                if self.kind(arg, fd, names) == CONN:
                    self.params.add((target.qualname, param))
