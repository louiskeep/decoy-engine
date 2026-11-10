"""Select the test files a set of changed source files can affect.

gates.sh used to grep test files for the changed module's dotted name. That misses tests which
reach changed code only through other modules (a pipeline importing a quality module at function
level). This helper builds a reverse import graph over src/ and tests/ with `ast` (every Import
and ImportFrom, function-level and relative included), expands the changed modules to all
transitive importers, and selects a test file when it is in that closure or names a module of the
changed set as text.

Usage: select_tests.py ROOT CHANGED_SRC_PATH...   (prints one test path per line)
Exit 2 on a parse or read failure so the gate fails closed and does not skip tests.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

SRC_ROOT = "src"
TESTS_ROOT = "tests"


def _module_name(rel: Path, base: Path) -> str:
    """Dotted name of repo-relative `rel`, counted from repo-relative directory `base`."""
    parts = list(rel.relative_to(base).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(tree: ast.AST, package: str) -> set[str]:
    """Dotted names a module imports. `package` is the module's own package ('' if top level)."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".") if package else []
                anchor = anchor[: len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                base = ".".join([*anchor, *([base] if base else [])])
            if base:
                out.add(base)
            # `from pkg import mod` imports pkg.mod when mod is a submodule.
            out.update(f"{base}.{a.name}" if base else a.name for a in node.names)
    return out


def build_graph(root: Path) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Return (importers, name -> repo-relative path) over src/ and tests/."""
    modules: dict[str, str] = {}  # node name -> repo-relative path
    imports: dict[str, set[str]] = {}  # node name -> raw imported names
    for top in (SRC_ROOT, TESTS_ROOT):
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root)
            # tests/ modules are keyed by their path from the repo root; src/ by the package path.
            name = _module_name(rel, Path(SRC_ROOT) if top == SRC_ROOT else Path("."))
            package = name if path.name == "__init__.py" else name.rpartition(".")[0]
            modules[name] = rel.as_posix()
            imports[name] = _imports(ast.parse(path.read_text(encoding="utf-8"), str(rel)), package)

    # Bare-name aliases for test helpers (`from _gates_harness import ...` works via rootdir/path).
    aliases: dict[str, list[str]] = defaultdict(list)
    for name in modules:
        if name.startswith(f"{TESTS_ROOT}."):
            aliases[name.rpartition(".")[2]].append(name)

    importers: dict[str, set[str]] = defaultdict(set)
    for name, raw in imports.items():
        for target in raw:
            if target in modules:
                importers[target].add(name)
            for alias in aliases.get(target, ()):
                importers[alias].add(name)
    return importers, modules


def closure(importers: dict[str, set[str]], seeds: set[str]) -> set[str]:
    seen = set(seeds)
    todo = deque(seeds)
    while todo:
        for dep in importers.get(todo.popleft(), ()):
            if dep not in seen:
                seen.add(dep)
                todo.append(dep)
    return seen


def select(root: Path, changed: list[str]) -> list[str]:
    importers, modules = build_graph(root)
    by_path = {p: n for n, p in modules.items()}
    seeds: set[str] = set()
    for rel in changed:
        name = by_path.get(rel)
        if name:
            seeds.add(name)
    reach = closure(importers, seeds)
    selected = {
        modules[n]
        for n in reach
        if modules[n].startswith(f"{TESTS_ROOT}/") and Path(modules[n]).name.startswith("test_")
    }
    # Text mentions (monkeypatch targets, string import paths) still count, as the old grep did.
    src_names = [n for n in seeds if not n.startswith(f"{TESTS_ROOT}.")]
    if src_names:
        pattern = re.compile(
            "(?:" + "|".join(re.escape(n) for n in sorted(src_names)) + r")(?![A-Za-z0-9_])"
        )
        for path in (root / TESTS_ROOT).rglob("test_*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                selected.add(path.relative_to(root).as_posix())
    return sorted(selected)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: select_tests.py ROOT CHANGED_PATH...", file=sys.stderr)
        return 2
    try:
        picked = select(Path(argv[1]), argv[2:])
        if picked:
            print("\n".join(picked))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        print(f"select_tests: cannot compute selection: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
