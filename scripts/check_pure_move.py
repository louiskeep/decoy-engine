"""Pure-move checker for H-1 Wave B engine slices (plan 1.3): replays Python's import
semantics (language reference, "The import system", "Naming and binding") and compares
statements with ``ast.dump``. Manifest (JSON): ``old``; ``new`` in execution order, with
``old`` when kept; ``map`` {moved name: defining path}; ``importers``; ``docstring_prose``;
``tests``; optional ``frozen_partners``. Paths are repo paths, never dotted module names.
A def is inert when undecorated and its definition-time expressions call nothing and read
only builtins the module does not rebind. Problems start with a rule tag and carry paths,
lines, statement kinds and names, never source text.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from pathlib import Path

try:
    from scripts import check_pure_move_scope as scope
except ImportError:  # run as a file: scripts/ is on sys.path, the repo root is not
    import check_pure_move_scope as scope

BUILTINS, DEFS, IMPORTS = scope.BUILTINS, scope.DEFS, scope.IMPORTS
binds, def_time, reads, shallow = scope.binds, scope.def_time, scope.reads, scope.shallow
Reader, Files, Only = Callable[[str], "str | None"], Iterable[str], "frozenset[str] | None"
_PENDING = ("5", "6")
_REQUIRED = ("old", "new", "map", "importers", "docstring_prose", "tests")
_IMPLICIT = {"__name__", "__file__", "__doc__", "__spec__", "__loader__", "__package__"}
_EFFECTS = (ast.Call, ast.Lambda, ast.Await, ast.NamedExpr, ast.Yield, ast.YieldFrom)
_DOCS = (ast.Module, ast.ClassDef, *DEFS)


def module_name(path: str) -> str:
    return path.removeprefix("src/").removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def _keys(stmt: ast.stmt) -> list[tuple]:
    level, src = (stmt.level, stmt.module or "") if isinstance(stmt, ast.ImportFrom) else (0, "")
    return [(level, src, a.name, a.asname) for a in stmt.names] if isinstance(stmt, IMPORTS) else []


class _Mod:
    def __init__(self, path: str, source: str, prose: bool) -> None:
        self.path, self.name = path, module_name(path)
        tree = ast.parse(source)
        for node in [n for n in ast.walk(tree) if isinstance(n, _DOCS)] if prose else [tree]:
            if ast.get_docstring(node, clean=False) is not None:
                node.body = node.body[1:] or [ast.Pass()]
        self.body = tree.body
        self.keys = [k for s in self.body for k in _keys(s)]
        self.lazy = (0, "__future__", "annotations", None) in self.keys
        self.binds = set().union(*map(binds, self.body))
        self.inert = {id(s) for s in self.body if self._inert(s)}
        self.ordered = [s for s in self.body if not (id(s) in self.inert or _keys(s))]

    def _inert(self, s: ast.stmt) -> bool:
        if not isinstance(s, DEFS) or s.decorator_list:
            return False
        calls = [n for e in def_time(s, self.lazy) for n in ast.walk(e) if isinstance(n, _EFFECTS)]
        return not calls and reads(s, self.lazy) <= BUILTINS - self.binds

    def targets(self, stmt: ast.stmt, known: dict[str, _Mod]) -> list[str]:
        """Manifest modules ``stmt`` imports, in the order Python loads them."""
        if not isinstance(stmt, IMPORTS):
            return []
        src = self.source(stmt) if isinstance(stmt, ast.ImportFrom) else None
        names = [src, *(a.name if src is None else f"{src}.{a.name}" for a in stmt.names)]
        return [n for n in dict.fromkeys(names) if n in known]

    def source(self, stmt: ast.ImportFrom) -> str:
        parts = self.name.split(".")[: None if self.path.endswith("__init__.py") else -1]
        parts = parts[: len(parts) - stmt.level + 1] if stmt.level else []
        return ".".join([*parts, *filter(None, [stmt.module])])

    def p(self, tag: str, stmt: ast.stmt, msg: str) -> str:
        names = ",".join(sorted(binds(stmt))) or "-"
        return f"{tag}: {self.path}:{stmt.lineno}: {type(stmt).__name__} {names}: {msg}"


def _graph(old: _Mod, known: dict[str, _Mod]) -> tuple[list[str], list]:
    """Rule 4: no cycle, no conditional link, every ordered statement reached from ``old``."""
    out, reach, trace, started = [], {}, [], set()
    for mod in known.values():
        nodes = [(s, n) for s in mod.body for n in [s, *shallow(s)] if mod.targets(n, known)]
        reach[mod.name] = {t for _, n in nodes for t in mod.targets(n, known)}
        out += [mod.p("4", s, "conditional manifest import") for s, n in nodes if s is not n]
    for name in [*known] * len(known):
        reach[name] = reach[name].union(*(reach[m] for m in reach[name]))
    out += [f"4: {known[n].path}: on an import cycle" for n in known if n in reach[n]]

    def run(mod: _Mod) -> None:
        started.add(mod.name)
        for stmt in mod.body:
            for target in (t for t in mod.targets(stmt, known) if t not in started):
                run(known[target])
            trace.append((mod, stmt))

    run(known[old.name]) if old.name in known else None
    lost = [(m, s) for m in known.values() if m.name not in started for s in m.ordered]
    return out + [m.p("4", s, "ordered; the old path never imports it") for m, s in lost], trace


def _order(old: _Mod, known: dict[str, _Mod], trace: list) -> list[str]:
    """Rules 1a, 1b, 1c and the import position check."""
    out, dump = [], ast.dump
    head = [(m, s) for m, s in trace if s in m.ordered]
    a, b = [dump(s) for s in old.ordered], [dump(s) for _, s in head]
    ops = SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    for _, i1, i2, j1, j2 in (op for op in ops if op[0] != "equal"):
        out += [old.p("1a", s, "base statement lost or moved") for s in old.ordered[i1:i2]]
        out += [m.p("1a", s, "not at its base position") for m, s in head[j1:j2]]
    inert = [(m, s) for m in [old, *known.values()] for s in m.body if id(s) in m.inert]
    count = {side: Counter(dump(s) for m, s in inert if (m is old) == side) for side in (1, 0)}
    extra = {1: count[1] - count[0], 0: count[0] - count[1]}
    out += [m.p("1b", s, "inert def unmatched") for m, s in inert if extra[m is old][dump(s)]]
    bound = {name: set(_IMPLICIT) for name in known}
    for mod, stmt in trace:
        src = mod.source(stmt) if isinstance(stmt, ast.ImportFrom) else ""
        early = {a.name for a in getattr(stmt, "names", []) if src in known} - bound.get(src, set())
        early = {n for n in early if f"{src}.{n}" not in known}
        late = reads(stmt, mod.lazy) - bound[mod.name] - (BUILTINS - mod.binds) | early
        out += [mod.p("1c", stmt, f"{n} read or imported before it is bound") for n in sorted(late)]
        bound[mod.name] |= binds(stmt)
    first = {k: i for i, s in reversed(list(enumerate(old.body))) for k in _keys(s)}
    for mod in known.values():
        lead = mod.body.index(mod.ordered[0]) if mod.ordered else len(mod.body)
        for j, stmt in enumerate(mod.body):
            i = min((first[k] for k in _keys(stmt) if k in first), default=None)
            prior = {dump(s) for s in old.ordered if i is not None and old.body.index(s) < i}
            late = [s for s in mod.ordered if mod.body.index(s) > j and dump(s) in prior]
            if late or (i is not None and not prior and j > lead):
                out.append(mod.p("import-position", stmt, "runs earlier or later than at base"))
    return out


def _names(old: _Mod, known: dict[str, _Mod], own: dict[str, str]) -> list[str]:
    """Rule 2: a base import or a moved name from its map owner; rule 3: one defining file."""
    out = []
    for mod in known.values():
        for stmt, (level, src, name, alias) in ((s, k) for s in mod.body for k in _keys(s)):
            linked = name in own and module_name(own[name]) == src and alias in (None, name)
            submodule = (f"{src}.{name}" if src else name) in known and alias is None
            if (level, src, name, alias) not in old.keys and (level or not (linked or submodule)):
                out.append(mod.p("2", stmt, f"{name} from neither a base import nor its map owner"))
    sites = [(n, m, s) for m in known.values() for s in m.body for n in sorted(binds(s, False))]
    files = {n: {m.path for k, m, _ in sites if k == n} for n, _, _ in sites}
    kept = known[old.name].path if old.name in known else old.path
    out += [f"3: {n}: defined in {len(f)} files" for n, f in sorted(files.items()) if len(f) > 1]
    out += [m.p("3", s, f"owner is {o}") for n, m, s in sites if m.path != (o := own.get(n, kept))]
    lost = [(n, p) for n, p in sorted(own.items()) if p not in files.get(n, ())]
    return out + [f"3: {p}: map assigns {n} here and it is not defined" for n, p in lost]


def check_move(
    manifest: dict, read_base: Reader, read_head: Reader, production_files: Files, only: Only = None
) -> list[str]:
    """Return the problems that keep ``manifest``'s move from being pure; empty when pure."""
    base_text = read_base(manifest.get("old", ""))
    kept = {module_name(p) for p in manifest.get("new", [])} & {module_name(manifest["old"])}
    if base_text is None or not kept or any(key not in manifest for key in _REQUIRED):
        raise ValueError(f"manifest needs {_REQUIRED}, old present at base and kept in new")
    prose = bool(manifest["docstring_prose"])
    old, known, out = _Mod(manifest["old"], base_text, prose), {}, []
    texts = {path: read_head(path) for path in manifest["new"]}
    for path, text in texts.items():
        out += [f"3: {path}: listed in new, absent at head"] if text is None else []
        known.update({} if text is None else {module_name(path): _Mod(path, text, prose)})
    problems, trace = _graph(old, known)
    out += problems + _order(old, known, trace)
    out += _names(old, known, manifest["map"]) + scope.scope_problems(
        base_text, texts, manifest["map"]
    )
    out = [p for p in out if only is None or p.split(":", 1)[0] in only]
    return out + [f"not-implemented: rule {r}" for r in _PENDING if only is None or r in only]


def _git(*args: str) -> str | None:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607 - fixed argv, no shell
    return done.stdout if done.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that a Wave B move slice is pure.")
    for flag in ("--manifest", "--base", "--head"):
        parser.add_argument(flag, required=True)
    a = parser.parse_args(argv)
    files = (_git("ls-tree", "-r", "--name-only", a.head, "src/decoy_engine") or "").split()
    base, head = (lambda p, r=r: _git("show", f"{r}:{p}") for r in (a.base, a.head))
    problems = check_move(json.loads(Path(a.manifest).read_text()), base, head, files)
    print("\n".join(problems) or "PURE")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
