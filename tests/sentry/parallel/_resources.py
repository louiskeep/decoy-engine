"""Static scan for tests that need the whole host, plus the collection dump plugin.

A test that asserts a wall-clock bound, a memory envelope, a CPU speedup ratio, an absolute
thread count, a fixed port or an in-process rlimit cannot share a machine with other tests, so
`scripts/dev/gates.sh` runs it in the serial pass (marker `serial`). The scan finds these per
test function, resolving calls through the module's import table (never by name text) and
following calls into local functions and `tests/**/_*.py` helper modules for two hops. A `Hit`
carries a kind and a line, never a value.

Stdlib only, so the sentry can load it by path. The hooks at the bottom make this module a
pytest plugin when named with `-p`: it dumps what the real collection saw (node ids, defining
function, whether the `serial` marker was present, files whose collection was skipped).
"""

from __future__ import annotations

import ast
import fnmatch
import inspect
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

FuncDef = ast.FunctionDef | ast.AsyncFunctionDef

CLOCKS = frozenset(
    {
        ("time", "perf_counter"),
        ("time", "perf_counter_ns"),
        ("time", "monotonic"),
        ("time", "monotonic_ns"),
        ("time", "time"),
        ("time", "time_ns"),
        ("time", "process_time"),
    }
)
ASSERT_CALLS = frozenset({"assertLess", "assertLessEqual", "assertGreater", "assertGreaterEqual"})
BOUND_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
PSUTIL_READS = frozenset({"memory_info", "memory_full_info", "memory_percent", "virtual_memory"})
PROC_FIELDS = ("VmHWM", "VmRSS")
# Names that read as a time, a rate or a memory figure. Lower-cased glob patterns.
TIME_NAMES = (
    "*elapsed*",
    "*wall*",
    "*_seconds",
    "*_s",
    "*_ms",
    "*_ns",
    "p50*",
    "p95*",
    "*speedup*",
    "*budget*",
    "*bound*",
)
MEMORY_NAMES = ("*rss*", "*hwm*", "*peak*", "*mem*cap*", "*_mb", "*cap*")
# Compared with any expression, whatever the operator.
PRESSURE_NAMES = ("*memory_pressure*", "*near_cap*")
HOPS = 2


@dataclass(frozen=True)
class Hit:
    kind: str
    line: int


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, p) for p in patterns)


class _Module:
    """One parsed module: its import table and every function it defines, by qualified name."""

    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        # local name -> (module, attribute); attribute is None for a plain module import
        self.imports: dict[str, tuple[str, str | None]] = {}
        self.funcs: dict[str, FuncDef] = {}
        self._index(self.tree.body, "")
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    key = a.asname or a.name.split(".")[0]
                    self.imports[key] = (a.name if a.asname else a.name.split(".")[0], None)
            elif isinstance(node, ast.ImportFrom):
                base = self._absolute(node)
                for a in node.names:
                    self.imports[a.asname or a.name] = (base, a.name)

    def _absolute(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        pkg = list(self.path.relative_to(self.root).with_suffix("").parts[:-1])
        keep = pkg[: len(pkg) - (node.level - 1)]
        return ".".join([*keep, *([node.module] if node.module else [])])

    def _index(self, body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[prefix + node.name] = node
            elif isinstance(node, ast.ClassDef):
                self._index(node.body, f"{prefix}{node.name}.")

    def resolve(self, func: ast.expr) -> tuple[str, str] | None:
        """`(module, name)` for a call target spelled through this module's import table."""
        if isinstance(func, ast.Name):
            got = self.imports.get(func.id)
            return (got[0], got[1]) if got and got[1] else None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            got = self.imports.get(func.value.id)
            if got is None:
                return None
            mod = got[0] if got[1] is None else f"{got[0]}.{got[1]}"
            return (mod, func.attr)
        return None


def _ignored_nodes(fn: ast.AST) -> set[int]:
    """Nodes under a `preexec_fn=` argument: they run in the child, not in this process."""
    skip: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "preexec_fn":
                    skip.update(id(n) for n in ast.walk(kw.value))
    return skip


class Scanner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._modules: dict[Path, _Module] = {}
        self._cache: dict[tuple[Path, str, int], frozenset[Hit]] = {}

    def module(self, path: Path) -> _Module:
        if path not in self._modules:
            self._modules[path] = _Module(path, self.root)
        return self._modules[path]

    def hits(self, path: Path, qualname: str, hops: int = HOPS) -> frozenset[Hit]:
        """Every resource use in the function and in what it calls, to `hops` calls deep."""
        key = (path, qualname, hops)
        if key in self._cache:
            return self._cache[key]
        self._cache[key] = frozenset()  # a call cycle ends here
        mod = self.module(path)
        fn = mod.funcs.get(qualname)
        out: set[Hit] = set()
        if fn is not None:
            out |= self._direct(mod, fn)
            if hops > 0:
                for target_path, target in self._callees(mod, qualname, fn):
                    out |= {
                        Hit(h.kind, fn.lineno) for h in self.hits(target_path, target, hops - 1)
                    }
        self._cache[key] = frozenset(out)
        return self._cache[key]

    def _callees(self, mod: _Module, qualname: str, fn: FuncDef) -> list[tuple[Path, str]]:
        cls = qualname.rpartition(".")[0]
        found: list[tuple[Path, str]] = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id in mod.funcs:
                found.append((mod.path, f.id))
            elif (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id in ("self", "cls")
                and cls
                and f"{cls}.{f.attr}" in mod.funcs
            ):
                found.append((mod.path, f"{cls}.{f.attr}"))
            else:
                target = mod.resolve(f)
                helper = self._helper_path(target[0]) if target else None
                if target and helper is not None:
                    found.append((helper, target[1]))
        return found

    def _helper_path(self, dotted: str) -> Path | None:
        """The `tests/**/_*.py` file a dotted import names, when it exists."""
        parts = dotted.split(".")
        if parts[0] != "tests" or not parts[-1].startswith("_"):
            return None
        path = self.root.joinpath(*parts).with_suffix(".py")
        return path if path.is_file() else None

    def _direct(self, mod: _Module, fn: FuncDef) -> set[Hit]:
        skip = _ignored_nodes(fn)
        tainted = _taint(mod, fn, skip)
        hits: set[Hit] = set()
        for node in ast.walk(fn):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Compare):
                _compare(mod, node, tainted, hits)
            elif isinstance(node, ast.Call):
                _call(mod, node, hits)
        return hits


def _compare(mod: _Module, node: ast.Compare, tainted: set[str], hits: set[Hit]) -> None:
    operands = [node.left, *node.comparators]
    if any(_named(o, PRESSURE_NAMES) for o in operands):
        hits.add(Hit("memory", node.lineno))
    if any(
        isinstance(o, ast.Call) and _dotted(o.func).endswith("active_count") for o in operands
    ) and any(isinstance(o, ast.Constant) for o in operands):
        hits.add(Hit("threads", node.lineno))
    if not any(isinstance(op, BOUND_OPS) for op in node.ops):
        return
    if any(_is_resource(mod, o, tainted, "time") for o in operands):
        hits.add(Hit("clock", node.lineno))
    if any(_is_resource(mod, o, tainted, "memory") for o in operands):
        hits.add(Hit("memory", node.lineno))


def _call(mod: _Module, node: ast.Call, hits: set[Hit]) -> None:
    if mod.resolve(node.func) == ("resource", "setrlimit"):
        hits.add(Hit("rlimit", node.lineno))
    if not isinstance(node.func, ast.Attribute):
        return
    if node.func.attr == "bind" and node.args and _fixed_port(node.args[0]):
        hits.add(Hit("port", node.lineno))
    if node.func.attr in ASSERT_CALLS:
        for kind in ("time", "memory"):
            if any(_is_resource(mod, a, set(), kind) for a in node.args):
                hits.add(Hit("clock" if kind == "time" else "memory", node.lineno))


def _taint(mod: _Module, fn: FuncDef, skip: set[int]) -> set[str]:
    """Names assigned from a clock or memory read, followed through simple assignments."""
    tainted: set[str] = set()
    for _ in range(4):
        before = len(tainted)
        for node in ast.walk(fn):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Assign):
                value, targets = node.value, node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                value, targets = node.value, [node.target]
            else:
                continue
            if value is not None and any(
                _is_resource(mod, value, tainted, kind) for kind in ("time", "memory")
            ):
                for t in targets:
                    tainted |= _base_names(t)
        if len(tainted) == before:
            break
    return tainted


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _base_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Attribute):
        return {target.attr, *_base_names(target.value)}
    if isinstance(target, ast.Subscript):
        return _base_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_base_names(e) for e in target.elts)) if target.elts else set()
    return set()


def _fixed_port(arg: ast.expr) -> bool:
    """`(host, N)` with a literal non-zero N."""
    return (
        isinstance(arg, ast.Tuple)
        and len(arg.elts) == 2
        and isinstance(arg.elts[1], ast.Constant)
        and isinstance(arg.elts[1].value, int)
        and arg.elts[1].value != 0
    )


def _names_in(expr: ast.AST) -> list[str]:
    """Identifier-like text in an expression: names, attributes, short string keys."""
    out: list[str] = []
    for n in ast.walk(expr):
        if isinstance(n, ast.Name):
            out.append(n.id)
        elif isinstance(n, ast.Attribute):
            out.append(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 40:
            out.append(n.value)
        elif isinstance(n, ast.keyword) and n.arg:
            out.append(n.arg)
    return out


def _named(expr: ast.AST, patterns: tuple[str, ...]) -> bool:
    return any(_matches(n, patterns) for n in _names_in(expr))


def _is_resource(mod: _Module, expr: ast.AST, tainted: set[str], kind: str) -> bool:
    """True when `expr` is, or derives from, a clock read (`time`) or a memory read (`memory`)."""
    for n in ast.walk(expr):
        if isinstance(n, ast.Call):
            target = mod.resolve(n.func)
            if kind == "time" and target in CLOCKS:
                return True
            if kind == "memory" and (
                (target is not None and target[0] == "psutil" and target[1] in PSUTIL_READS)
                or (isinstance(n.func, ast.Attribute) and n.func.attr in PSUTIL_READS)
            ):
                return True
        if kind == "memory":
            if isinstance(n, ast.Attribute) and n.attr == "ru_maxrss":
                return True
            if isinstance(n, ast.Constant) and n.value in PROC_FIELDS:
                return True
        if isinstance(n, ast.Name) and n.id in tainted:
            return True
    return _named(expr, TIME_NAMES if kind == "time" else MEMORY_NAMES)


# --- judging: the scan and the registry against what the real collection saw ---------------


@dataclass(frozen=True)
class Collected:
    """One collected test as the dump plugin saw it."""

    nodeid: str
    source: str  # repo-relative file that defines the test function
    qualname: str
    serial: bool

    @property
    def file(self) -> str:
        return self.nodeid.split("::", 1)[0]


def parse_marker_expr(gates_text: str) -> str:
    """The `-m` expression of gates.sh's PYTEST_ARGS: the one reader of that line.

    Strict on purpose: exactly one `PYTEST_ARGS=` line, and it must be the whole form
    `PYTEST_ARGS=(-m '<expr>')`. Any other shape raises, so a format change breaks every
    consumer here instead of being half-read by a looser copy elsewhere."""
    pins = [ln for ln in gates_text.splitlines() if ln.startswith("PYTEST_ARGS=")]
    m = re.fullmatch(r"PYTEST_ARGS=\(-m '([^']+)'\)", pins[0]) if len(pins) == 1 else None
    if m is None:
        raise ValueError("gates.sh has no single PYTEST_ARGS=(-m '<expr>') line")
    return m.group(1)


def registry_matches(prefix: str, nodeid: str) -> bool:
    """Exact node-id prefixes: the id itself, its parametrized cases, or a path under a `dir/`."""
    if prefix.endswith("/"):
        return nodeid.startswith(prefix)
    return nodeid == prefix or nodeid.startswith((prefix + "[", prefix + "::"))


def unmarked_registry_entries(
    registry: list[str], items: list[Collected], unverifiable_files: dict[str, str]
) -> tuple[list[str], list[str]]:
    """`(failures, unverified)` for the required-serial registry.

    Every collected item a prefix matches must carry `serial`. A prefix that matches nothing
    fails unless its exact file was skipped or errored at collection, which is reported as
    unverified with the reason instead."""
    failures: list[str] = []
    unverified: list[str] = []
    for prefix in registry:
        matched = [i for i in items if registry_matches(prefix, i.nodeid)]
        if not matched:
            file = prefix.split("::", 1)[0]
            if file in unverifiable_files:
                unverified.append(f"{prefix}: {unverifiable_files[file]}")
            else:
                failures.append(f"registry entry matches no collected test: {prefix}")
        failures += [
            f"registry test lacks the serial mark: {i.nodeid}" for i in matched if not i.serial
        ]
    return failures, unverified


def registry_problems(failures: list[str], unverified: list[str], strict: bool) -> list[str]:
    """Failures, plus every unverifiable entry when `strict` (the gate venv has every extra, so
    nothing may skip at collection there; a thinner venv only reports them)."""
    return (
        [*failures, *(f"unverified in strict mode: {u}" for u in unverified)]
        if strict
        else failures
    )


def removed_without_reason(base_ids: set[str], current: dict[str, list[str]]) -> list[str]:
    """Registry ids the base had that are gone and not listed under `removed` with a reason."""
    kept = set(current["serial"])
    reasons = {e.split("|", 1)[0]: e.split("|", 1)[1] for e in current["removed"] if "|" in e}
    return sorted(i for i in base_ids - kept if len(reasons.get(i, "").strip()) < 15)


def resource_failures(
    items: list[Collected],
    scan: Callable[[Collected], frozenset[Hit]],
    allowlist: set[str],
) -> tuple[list[str], list[str]]:
    """`(failures, stale)`: unmarked resource users off the allowlist, and allowlist rows to drop
    (a row is stale once its function carries the mark or no longer matches the scan).

    The allowlist key is `<defining file>::<qualname>`; it names the function, so every
    parametrized case of it is covered and a new function of the same file is not."""
    failures: list[str] = []
    unmarked: set[str] = set()
    for item in items:
        hits = scan(item)
        if not hits or item.serial:
            continue
        key = f"{item.source}::{item.qualname}"
        unmarked.add(key)
        if key not in allowlist:
            kinds = ", ".join(sorted({h.kind for h in hits}))
            failures.append(
                f"{item.nodeid}: uses a host resource ({kinds}) without the serial mark"
            )
    stale = sorted(k for k in allowlist if k not in unmarked)
    return failures, stale


# --- what the real collection saw: this module doubles as a pytest plugin ------------------

DUMP_ENV = "DECOY_SERIAL_DUMP"
_state: dict[str, list[dict[str, object]]] = {"items": [], "skipped": [], "errors": []}


def pytest_collectreport(report):  # type: ignore[no-untyped-def]
    if report.skipped:
        lr = report.longrepr
        reason = lr[2] if isinstance(lr, tuple) and len(lr) == 3 else "skipped"
        _state["skipped"].append(
            {"file": str(report.nodeid), "reason": str(reason).removeprefix("Skipped: ")}
        )
    elif report.failed:
        _state["errors"].append({"file": str(report.nodeid), "reason": "collection error"})


def _relative_source(fn) -> str:  # type: ignore[no-untyped-def]
    """The repo-relative file defining `fn`; empty when there is none (a module name would not do:
    without a package `__init__` pytest names the module by its bare file name)."""
    if fn is None:
        return ""
    try:
        src = inspect.getsourcefile(fn)
    except TypeError:
        return ""
    return Path(os.path.relpath(src)).as_posix() if src else ""


def pytest_collection_finish(session):  # type: ignore[no-untyped-def]
    for item in session.items:
        fn = getattr(item, "function", None)
        if fn is not None:
            try:
                fn = inspect.unwrap(fn)  # a decorated test is scanned where it is written
            except ValueError:
                pass
        _state["items"].append(
            {
                "nodeid": item.nodeid,
                "source": _relative_source(fn),
                "qualname": getattr(fn, "__qualname__", ""),
                "serial": item.get_closest_marker("serial") is not None,
            }
        )
    out = os.environ.get(DUMP_ENV)
    if out:
        Path(out).write_text(json.dumps(_state), encoding="utf-8")
