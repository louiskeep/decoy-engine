"""Pick the test files a diff can affect, by what each test imports.

Called by `gates.sh changed_tests` with the changed code files on stdin, one per line, each
prefixed `s ` (a src module) or `o ` (any other code file). Prints test file paths, one per line.

A test file is selected when any of these hold:

* it imports a changed file: `import a.b`, `from a import b`, `from . import b`,
  `from ._b import x`, resolved with `ast` so spelling and line continuations do not matter;
* it imports, transitively through test helpers and src modules (in-function imports count),
  a module that imports a changed file;
* a changed `conftest.py` sits in its directory or above it;
* it has a `from <parent> import` line and mentions the changed module's leaf name as a word
  (the old grep rule, kept so the selection never shrinks);
* it mentions a changed src module's dotted name, or another changed file's basename, as text (strings
  such as `mock.patch("a.b.f")` and script names in subprocess calls carry no import);
* it does not parse, because a file we cannot read cannot be ruled out.

Over-selecting is the safe error. Dropping a dependent is the failure this script exists to stop.
"""

from __future__ import annotations

import ast
import os
import re
import sys

TESTS = "tests"


def _walk_py(root: str, top: str) -> list[str]:
    out: list[str] = []
    for d, dirs, files in os.walk(os.path.join(root, top)):
        dirs[:] = [x for x in dirs if x != "__pycache__" and not x.startswith(".")]
        out.extend(os.path.relpath(os.path.join(d, f), root) for f in files if f.endswith(".py"))
    return sorted(out)


def _module_parts(path: str, strip: str = "") -> list[str]:
    p = path[len(strip) :] if strip and path.startswith(strip) else path
    parts = p[: -len(".py")].split("/")
    return parts[:-1] if parts[-1] == "__init__" else parts


def _prefixes(name: str) -> set[str]:
    parts = name.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)}


def imported_names(source: str, path: str) -> set[str] | None:
    """Every dotted name the file imports, plus `pkg.name` for each `from pkg import name`.

    None when the file does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    here = path.split("/")[:-1]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(here) - (node.level - 1)
                base = here[: max(keep, 0)]
                mod = ".".join([*base, *([node.module] if node.module else [])])
            else:
                mod = node.module or ""
            if mod:
                names.add(mod)
            for a in node.names:
                if a.name != "*":
                    names.add(f"{mod}.{a.name}" if mod else a.name)
    return names


def select(root: str, changed: list[tuple[str, str]], src_strip: str) -> set[str]:
    # src modules join the graph: a test that imports `synthesize` reaches what `synthesize`
    # imports, including imports inside function bodies (ast.walk visits them).
    src_top = src_strip.strip("/")
    src_files = _walk_py(root, src_top) if src_top else []
    files = _walk_py(root, TESTS) + src_files
    is_src = set(src_files)
    texts: dict[str, str] = {}
    for f in files:
        with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
            texts[f] = fh.read()
    imports: dict[str, set[str] | None] = {}
    for f in files:
        try:
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                imports[f] = imported_names(fh.read(), f[len(src_strip) :] if f in is_src else f)
        except (UnicodeDecodeError, OSError):
            imports[f] = None

    keys: set[str] = set()  # dotted names whose import means "depends on a changed file"
    needles: list[re.Pattern[str]] = []
    from_leaf: list[tuple[re.Pattern[str], re.Pattern[str]]] = []
    conftest_dirs: list[str] = []
    selected: set[str] = set()
    for kind, path in changed:
        base = path.rsplit("/", 1)[-1]
        if kind == "o":
            needles.append(re.compile(re.escape(base)))
        if path.endswith(".py"):
            if kind == "s":
                mod = ".".join(_module_parts(path, src_strip))
                keys.add(mod)
                parent, _, leaf = mod.rpartition(".")
                if parent:
                    from_leaf.append(
                        (
                            re.compile(rf"from[ \t]+{re.escape(parent)}[ \t]+import"),
                            re.compile(rf"(?<![A-Za-z0-9_]){re.escape(leaf)}(?![A-Za-z0-9_])"),
                        )
                    )
                needles.append(re.compile(re.escape(mod) + r"(?![A-Za-z0-9_])"))
            else:
                parts = _module_parts(path)
                keys.update(".".join(parts[i:]) for i in range(len(parts)))
                if base == "conftest.py":
                    conftest_dirs.append(path.rsplit("/", 1)[0] if "/" in path else "")
        if path in imports:
            selected.add(path)

    def hit(f: str) -> bool:
        names = imports[f]
        if names is None:
            return True
        if any(_prefixes(n) & keys for n in names):
            return True
        if f in is_src:
            return False
        if any(n.search(texts[f]) for n in needles):
            return True
        if any(a.search(texts[f]) and b.search(texts[f]) for a, b in from_leaf):
            return True
        return any(f.startswith(d + "/") if d else True for d in conftest_dirs)

    # Fixpoint: a helper that depends on a changed file is itself a changed file for its importers.
    pending = [f for f in files if f not in selected]
    grew = True
    while grew:
        grew = False
        for f in list(pending):
            if hit(f):
                selected.add(f)
                pending.remove(f)
                grew = True
                if f in is_src:
                    keys.add(".".join(_module_parts(f, src_strip)))
                elif os.path.basename(f)[:5] != "test_":
                    keys.update(
                        ".".join(_module_parts(f)[i:]) for i in range(len(_module_parts(f)))
                    )
    return {f for f in selected if os.path.basename(f).startswith("test_")}


def main(argv: list[str]) -> int:
    root, strip = "", "src/"
    it = iter(argv)
    for a in it:
        if a == "--root":
            root = next(it)
        elif a == "--src-strip":
            strip = next(it)
        else:
            print(f"_select_tests: unknown argument {a}", file=sys.stderr)
            return 64
    if not root:
        print("_select_tests: --root is required", file=sys.stderr)
        return 64
    changed: list[tuple[str, str]] = []
    for line in sys.stdin:
        line = line.rstrip("\n")
        if len(line) > 2 and line[1] == " " and line[0] in "so":
            changed.append((line[0], line[2:]))
    for f in sorted(select(root, changed, strip)):
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
