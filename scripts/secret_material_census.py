"""Derive the census of engine functions that hold secret material in a frame.

`traceback.TracebackException(capture_locals=True)` and error trackers render every local of
every frame a traceback keeps, including parameters and closure cells. This script lists, from
the AST of `src/decoy_engine` alone (no hand-picked list), every function (`def` / `async def`;
lambdas excluded) where secret material can be bound to a name its frame keeps:

- a parameter whose name is secret-shaped and whose annotation is bytes-like
  (`bytes`, `bytearray`, `memoryview`, also inside `Optional`/unions);
- an assigned local (assignment, annotated or augmented assignment, walrus, `for`/`with`
  target, tuple unpacking) whose name is secret-shaped and whose value is secret-bearing:
  an environment read, a `FileRole.SECRET` read, the contents of an opaque box (`.open()`),
  a call to a package function annotated to return bytes, a bytes-producing operation
  (`bytes()`, `bytes.fromhex`, `.encode()`, `.digest()`, base64 decode/encode, slicing or
  `+` of tracked material), an attribute annotated bytes in a package class, or an alias of
  another tracked name in the same frame;
- a closure cell: a nested function that reads a tracked name of its enclosing function
  (a frame's `f_locals` includes its free variables);
- every call of `os.environ.get` / `os.getenv` / `os.environ[...]` whose variable name holds
  KEY, SECRET or TOKEN (module string constants resolved) or is not a constant;
- every call that passes `FileRole.SECRET`.

The sentry `tests/sentry/test_secret_material_census.py` re-derives this list and compares it
with the committed census (`tests/sentry/secret_material_census.json`), where each entry carries
a reviewed verdict. Pattern: a static-analysis census ratchet in the spirit of Bandit's AST
plugins (hard-coded secrets, B105-B107) and Semgrep taint sources, restricted to the frame
locals a traceback renders.

    python scripts/secret_material_census.py            # print the derived list
    python scripts/secret_material_census.py --write    # refresh the census, keep verdicts
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "decoy_engine"
CENSUS = REPO / "tests" / "sentry" / "secret_material_census.json"

SECRET_NAME = re.compile(
    r"key|secret|master|signing|mask_key|password|token|seed_material|seed|ikm|prk|okm|material"
    r"|root",
    re.IGNORECASE,
)
ENV_SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN")
VERDICT_TEXT = {
    "wiped": "the frame drops the name before any raise propagates: the function body is a "
    "`try` whose `except BaseException:` deletes it and re-raises (for a local or an "
    "environment read, its binding is the statement right before the `try`); a closure cell "
    "is wiped by its enclosing function and the nested function never escapes it",
    "boxed": "the material is held only in a `decoy_engine._opaque.OpaqueBox` (fixed repr) and "
    "unboxed inside the call expression that consumes it; a boxed parameter is boxed and "
    "deleted by the first two statements",
    "unbound": "the value is consumed inside an expression and never bound to a name",
    "hidden": "a class attribute a repr would print: the dataclass field is `repr=False` or "
    "the class defines its own `__repr__`",
    "contained": "the function runs only under the admission boundary `_call_closed`, which "
    "re-raises a fresh exception with no traceback through it",
    "no-raise": "no raise is reachable while the name is live; the reason says why",
    "not-secret": "the name matched but the value is not secret material; the reason says why",
}
BYTES_TYPES = {"bytes", "bytearray", "memoryview"}
BYTES_METHODS = {
    "digest",
    "encode",
    "fromhex",
    "tobytes",
    "b64decode",
    "urlsafe_b64decode",
    "b64encode",
    "urlsafe_b64encode",
    "b32decode",
    "b16decode",
    "unhexlify",
}


@dataclass(frozen=True, order=True)
class Entry:
    file: str
    function: str
    name: str
    arrival: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.file, self.function, self.name, self.arrival)


def _annotation_is_bytes(node: ast.expr | None) -> bool:
    if node is None:
        return False
    # A callable that returns bytes is not bytes (`Callable[[str], bytes]`).
    if isinstance(node, ast.Subscript) and _dotted(node.value).endswith("Callable"):
        return False
    if isinstance(node, ast.BinOp):
        return _annotation_is_bytes(node.left) or _annotation_is_bytes(node.right)
    if isinstance(node, ast.Subscript) and _dotted(node.value).endswith(("Optional", "Union")):
        inner = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        return any(_annotation_is_bytes(e) for e in inner)
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in BYTES_TYPES:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in BYTES_TYPES:
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(re.search(rf"\b{t}\b", sub.value) for t in BYTES_TYPES):
                return True
    return False


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


class _PackageIndex:
    """Package-wide facts: bytes-returning functions, bytes attributes, string constants."""

    def __init__(self, trees: dict[str, ast.Module]) -> None:
        self.bytes_functions: set[str] = set()
        self.bytes_attributes: set[str] = set()
        self.constants: dict[str, dict[str, str]] = {}
        for rel, tree in trees.items():
            consts: dict[str, str] = {}
            for node in tree.body:
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets, value = [node.target], node.value
                else:
                    continue
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    for t in targets:
                        if isinstance(t, ast.Name):
                            consts[t.id] = value.value
            self.constants[rel] = consts
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _annotation_is_bytes(node.returns):
                        self.bytes_functions.add(node.name)
                elif isinstance(node, ast.AnnAssign) and _annotation_is_bytes(node.annotation):
                    if isinstance(node.target, ast.Attribute):
                        self.bytes_attributes.add(node.target.attr)
                    elif isinstance(node.target, ast.Name):
                        self.bytes_attributes.add(node.target.id)
        self.all_constants: dict[str, str] = {}
        for consts in self.constants.values():
            self.all_constants.update(consts)

    def env_name(self, rel: str, node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, (ast.Name, ast.Attribute)):
            ident = node.id if isinstance(node, ast.Name) else node.attr
            return self.constants.get(rel, {}).get(ident, self.all_constants.get(ident))
        return None


def _is_env_read(node: ast.AST) -> ast.expr | None:
    """The variable-name argument of an environment read, or None when `node` is not one."""
    if isinstance(node, ast.Call):
        dotted = _dotted(node.func)
        if dotted in {
            "os.environ.get",
            "environ.get",
            "os.getenv",
            "getenv",
            "os.environ.pop",
            "environ.pop",
        }:
            return node.args[0] if node.args else ast.Constant(value=None)
    if isinstance(node, ast.Subscript) and _dotted(node.value) in {"os.environ", "environ"}:
        if isinstance(node.ctx, ast.Load):
            return node.slice
    return None


def _passes_secret_role(call: ast.Call) -> bool:
    return any(
        _dotted(arg) in {"FileRole.SECRET", "fileaccess.FileRole.SECRET"}
        for arg in [*call.args, *(k.value for k in call.keywords)]
    )


class _FunctionScan:
    def __init__(self, rel: str, index: _PackageIndex) -> None:
        self.rel = rel
        self.index = index
        self.entries: list[Entry] = []

    def _value_arrival(self, value: ast.expr, tracked: dict[str, str]) -> str | None:
        """How a secret-bearing value arrives, or None when the value is not one."""
        if _is_env_read(value) is not None:
            return "env"
        for sub in ast.walk(value):
            if _is_env_read(sub) is not None:
                return "env"
            if isinstance(sub, ast.Call) and _passes_secret_role(sub):
                return "secret-file"
        if isinstance(value, ast.Call) and _dotted(value.func) != "int.from_bytes":
            name = _call_name(value)
            if name == "open" and isinstance(value.func, ast.Attribute):
                return "unboxed"
            if name in BYTES_TYPES or name in BYTES_METHODS:
                return f"bytes-op:{name}"
            if name in self.index.bytes_functions:
                return f"call:{name}"
        if isinstance(value, ast.Attribute) and value.attr in self.index.bytes_attributes:
            if SECRET_NAME.search(value.attr):
                return f"attr:{value.attr}"
        if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Call):
            if _call_name(value.value) == "open":
                return "unboxed"
        if isinstance(value, ast.Subscript):  # a slice of material is material
            inner = self._value_arrival(value.value, tracked)
            if inner is not None:
                return inner
        if isinstance(value, ast.BinOp):
            inner = self._value_arrival(value.left, tracked) or self._value_arrival(
                value.right, tracked
            )
            if inner is not None:
                return inner
        for sub in ast.walk(value):
            if isinstance(sub, ast.Name) and sub.id in tracked:
                if isinstance(value, ast.Name) or _derives_material(value):
                    return f"alias:{sub.id}"
        return None

    def scan(
        self, fn: ast.FunctionDef | ast.AsyncFunctionDef, qual: str, outer: dict[str, str]
    ) -> None:
        tracked: dict[str, str] = {}
        args = fn.args
        for arg in [
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
            *(a for a in (args.vararg, args.kwarg) if a is not None),
        ]:
            if not SECRET_NAME.search(arg.arg) or arg.arg in {"self", "cls"}:
                continue
            if arg.annotation is None or _annotation_is_bytes(arg.annotation):
                arrival = "parameter" if arg.annotation is not None else "parameter:unannotated"
                tracked[arg.arg] = arrival
                self._add(qual, arg.arg, arrival, arg.lineno)

        own = _own_nodes(fn)
        loaded = {n.id for n in own if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        bound = {n.id for n in own if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        bound |= {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
        for name in sorted(loaded - bound):
            if name in outer:
                tracked[name] = "closure"
                self._add(qual, name, f"closure:{outer[name]}", fn.lineno)

        # Two passes so aliases of names bound later in source order still resolve.
        for _ in range(2):
            for node in own:
                for target, value in _bindings(node):
                    if not SECRET_NAME.search(target.id):
                        continue
                    arrival = self._value_arrival(value, tracked)
                    if arrival is not None and target.id not in tracked:
                        tracked[target.id] = arrival
                        self._add(qual, target.id, arrival, target.lineno)

        for node in own:
            env_arg = _is_env_read(node)
            if env_arg is not None:
                name = self.index.env_name(self.rel, env_arg)
                if name is None or ENV_SECRET_NAME.search(name):
                    self._add(qual, f"<env:{name or 'dynamic'}>", "env-read", node.lineno)
            if isinstance(node, ast.Call) and _passes_secret_role(node):
                self._add(qual, "<FileRole.SECRET>", "secret-read", node.lineno)

        for node in own:
            for target, value in _bindings(node):
                if isinstance(value, ast.Call) and _call_name(value) == "OpaqueBox":
                    inner = [self._value_arrival(a, tracked) for a in value.args]
                    inner += [
                        tracked[a.id]
                        for a in value.args
                        if isinstance(a, ast.Name) and a.id in tracked
                    ]
                    found = next((i for i in inner if i), None)
                    if found is not None:
                        self._add(qual, target.id, f"box:{found}", target.lineno)

        scope = {**outer, **tracked}
        for lam in _own_lambdas(fn):
            params = {a.arg for a in ast.walk(lam.args) if isinstance(a, ast.arg)}
            for sub in ast.walk(lam.body):
                if isinstance(sub, ast.Name) and sub.id in scope and sub.id not in params:
                    self._add(f"{qual}.<lambda>", sub.id, f"closure:{scope[sub.id]}", lam.lineno)

        for child in _child_functions(fn):
            self.scan(child, f"{qual}.{child.name}", scope)

    def _add(self, qual: str, name: str, arrival: str, line: int) -> None:
        self.entries.append(Entry(self.rel, qual, name, arrival, line))


def _derives_material(value: ast.expr) -> bool:
    if isinstance(value, (ast.Subscript, ast.BinOp, ast.Starred, ast.Tuple, ast.List)):
        return True
    if isinstance(value, ast.Call):
        name = _call_name(value)
        return name in BYTES_TYPES or name in BYTES_METHODS
    return isinstance(value, ast.IfExp)


def _bindings(node: ast.AST) -> Iterator[tuple[ast.Name, ast.expr]]:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield from _unpack(target, node.value)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
        yield from _unpack(node.target, node.value)
    elif isinstance(node, ast.NamedExpr):
        yield node.target, node.value
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        yield from _unpack(node.target, node.iter)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                yield from _unpack(item.optional_vars, item.context_expr)


def _unpack(target: ast.expr, value: ast.expr) -> Iterator[tuple[ast.Name, ast.expr]]:
    if isinstance(target, ast.Name):
        yield target, value
    elif isinstance(target, (ast.Tuple, ast.List)):
        values = value.elts if isinstance(value, (ast.Tuple, ast.List)) else None
        for i, elt in enumerate(target.elts):
            inner = values[i] if values is not None and i < len(values) else value
            yield from _unpack(elt.value if isinstance(elt, ast.Starred) else elt, inner)


def _child_functions(fn: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for child in ast.iter_child_nodes(fn):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child
        elif not isinstance(child, (ast.ClassDef, ast.Lambda)):
            yield from _child_functions(child)


def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    """Nodes of `fn`'s body, not descending into nested functions, classes or lambdas."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.arguments):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _own_lambdas(fn: ast.AST) -> Iterator[ast.Lambda]:
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Lambda):
            yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _fields(rel: str, tree: ast.Module) -> Iterator[Entry]:
    """Class attributes annotated bytes with a secret-shaped name: a repr can print them."""
    for qual, node in _walk_classes(tree):
        for stmt in node.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and SECRET_NAME.search(stmt.target.id)
                and _annotation_is_bytes(stmt.annotation)
            ):
                yield Entry(rel, qual, stmt.target.id, "field", stmt.lineno)


def _walk_classes(tree: ast.AST, prefix: str = "") -> Iterator[tuple[str, ast.ClassDef]]:
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield f"{prefix}{node.name}", node
            yield from _walk_classes(node, f"{prefix}{node.name}.")
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _walk_classes(node, prefix)


def _walk_scopes(
    tree: ast.AST, prefix: str = ""
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            yield from _walk_scopes(node, f"{prefix}{node.name}.")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"{prefix}{node.name}", node
        else:
            yield from _walk_scopes(node, prefix)


def derive(root: Path = SRC) -> list[Entry]:
    """Every secret-material binding under `root`, sorted; deterministic for a given tree."""
    base = root.parent
    trees = {
        path.relative_to(base).as_posix(): ast.parse(path.read_text(encoding="utf-8"), str(path))
        for path in sorted(root.rglob("*.py"))
    }
    index = _PackageIndex(trees)
    entries: list[Entry] = []
    for rel, tree in trees.items():
        scan = _FunctionScan(rel, index)
        for qual, fn in _walk_scopes(tree):
            scan.scan(fn, qual, {})
        entries.extend(scan.entries)
        entries.extend(_fields(rel, tree))
    return sorted(set(entries))


def drift(
    entries: list[Entry], rows: list[dict[str, Any]]
) -> tuple[list[Entry], list[dict[str, Any]]]:
    """(derived bindings the census lacks, census rows the code no longer bears out)."""
    by_identity = {(r["file"], r["function"], r["name"], r["arrival"]): r for r in rows}
    derived = {e.identity: e for e in entries}
    missing = [e for e in entries if e.identity not in by_identity]
    stale = [
        r for key, r in by_identity.items() if key not in derived or derived[key].line != r["line"]
    ]
    return missing, stale


def load_census(path: Path = CENSUS) -> list[dict[str, object]]:
    return list(json.loads(path.read_text(encoding="utf-8"))["entries"])


def _write(entries: list[Entry], path: Path = CENSUS) -> int:
    old = {}
    if path.exists():
        old = {(e["file"], e["function"], e["name"], e["arrival"]): e for e in load_census(path)}
    rows = []
    for entry in entries:
        prior = old.get(entry.identity, {})
        rows.append(
            {
                "file": entry.file,
                "function": entry.function,
                "name": entry.name,
                "arrival": entry.arrival,
                "line": entry.line,
                "verdict": prior.get("verdict", "unreviewed"),
                "reason": prior.get("reason", ""),
            }
        )
    doc = {
        "about": "Derived by scripts/secret_material_census.py; verdicts are reviewed by hand.",
        "verdicts": VERDICT_TEXT,
        "entries": rows,
    }
    path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return sum(1 for r in rows if r["verdict"] == "unreviewed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="refresh the committed census")
    opts = parser.parse_args(argv)
    entries = derive()
    if opts.write:
        unreviewed = _write(entries)
        print(f"{len(entries)} entries, {unreviewed} unreviewed -> {CENSUS.relative_to(REPO)}")
        return 0
    for e in entries:
        print(f"{e.file}:{e.line}\t{e.function}\t{e.name}\t{e.arrival}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
