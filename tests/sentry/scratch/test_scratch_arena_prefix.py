"""The scratch arena's names and creation choke points (F25 S1b, plan 4.1 and 4.2).

The arena prefix, its pending prefix and its marker name are spelled only in
`fileaccess/_scratch.py`, and `open` names a root the sweep's exact pattern matches.
`[create_choke_point]`, on `tests.sentry.fa.scan` and its resolver (aliases, `getattr`,
re-exports): the arena modules create nothing themselves, `_output.py` creates only inside its
named raw functions, and its two private entry points each pass their one fixed restorer.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.sentry.fa.resolve import ProjectIndex, Scope, build_import_map, module_name, resolve
from tests.sentry.fa.scan import scan_tree

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
ARENA = "decoy_engine/fileaccess/_scratch.py"
ARENA_MODULES = frozenset({ARENA, "decoy_engine/fileaccess/_scratch_sweep.py"})
OUTPUT = "decoy_engine/fileaccess/_output.py"
OWNED_NAMES = frozenset({"ARENA_PREFIX", "_PENDING_PREFIX", "_MARKER", "_MAGIC"})
# The sweep's step 1 test (plan 4.4), used with `re.fullmatch`.
SWEEP_NAME = re.compile(r"decoy-arena-v1-[0-9a-f]{16}")
ARENA_BANNED = frozenset(
    {"os.mkdir", "os.makedirs", "tempfile.mkdtemp", "Path.mkdir", "os.chmod", "os.fchmod"}
)
CREATE_FLAGS = frozenset({"os.O_CREAT", "decoy_engine.fileaccess._output._FILE_CREATE"})
# Plan 4.2: every raw creation in `_output.py` sits in one of these, plus the one flag definition.
OUTPUT_RAW = frozenset(
    {
        "make_stage_dir_at",
        "_create_private_core",
        "create_output_file_at",
        "RootedOutputDir._open_parent",
        "RootedOutputDir.ensure_dir",
    }
)
PRIVATE_ENTRIES = {
    "create_private_at": ["_restore_always"],
    "create_private_child_at": ["_restore_missing_owner_bits"],
}

Walked = Iterator[tuple[str, ast.AST, Scope]]


def _modules(src: Path) -> Iterator[tuple[str, str, ast.Module, bool]]:
    for path in sorted((src / "decoy_engine").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        yield (
            path.relative_to(src).as_posix(),
            module_name(path, src),
            tree,
            path.name == "__init__.py",
        )


def _walk(tree: ast.Module, current: str, is_package: bool) -> Walked:
    """Every node with its function's qualified name ("<module>" at top) and import scope."""

    def visit(body: list[ast.stmt], qual: str, scope: Scope) -> Walked:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = scope.child(build_import_map(stmt.body, current, is_package))
                name = stmt.name if qual == "<module>" else f"{qual}.{stmt.name}"
                yield from visit(stmt.body, name, inner)
                continue
            for node in ast.walk(stmt):
                yield qual, node, scope

    yield from visit(tree.body, "<module>", Scope(build_import_map(tree.body, current, is_package)))


def prefix_findings(src: Path) -> list[str]:
    """Spellings of the arena's names outside `_scratch.py`: a `decoy-arena` literal (any
    `prefix=` value included), `ARENA_PREFIX` read or looked up, or a name constant imported
    from the arena module (`_MARKER` and `_MAGIC` are common private names elsewhere)."""
    out: list[str] = []
    for rel, _name, tree, _pkg in _modules(src):
        if rel == ARENA:
            continue
        for node in ast.walk(tree):
            text = node.value if isinstance(node, ast.Constant) else None
            if (
                (isinstance(text, str) and ("decoy-arena" in text or text == "ARENA_PREFIX"))
                or (isinstance(text, bytes) and b"decoy-arena" in text)
                or (isinstance(node, ast.Name) and node.id == "ARENA_PREFIX")
                or (isinstance(node, ast.Attribute) and node.attr == "ARENA_PREFIX")
                or (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or "").endswith(("fileaccess._scratch", "fileaccess"))
                    and any(a.name in OWNED_NAMES for a in node.names)
                )
            ):
                out.append(f"{rel}:{getattr(node, 'lineno', 0)}: an arena name outside {ARENA}")
    return out


def choke_point_findings(src: Path) -> list[str]:
    """Creations outside the two choke points, and a private entry point's restorer changed."""
    out = [
        f"{s.module}:{s.line} {s.function}: {s.sink}"
        for s in scan_tree(src)[0]
        if (s.module in ARENA_MODULES and s.sink in ARENA_BANNED)
        or (s.module == OUTPUT and s.sink == "os.mkdir" and s.function not in OUTPUT_RAW)
    ]
    index = ProjectIndex("decoy_engine")
    modules = list(_modules(src))
    for _rel, name, tree, pkg in modules:
        index.add(name, tree, pkg)
    core_calls: dict[str, list[str]] = {}
    for rel, name, tree, pkg in modules:
        definition = _flag_definition(tree) if rel == OUTPUT else set()
        for qual, node, scope in _walk(tree, name, pkg):
            where = f"{rel}:{getattr(node, 'lineno', 0)} {qual}"
            if _is_create_flag(rel, node, scope, index) and id(node) not in definition:
                if rel in ARENA_MODULES or (rel == OUTPUT and qual not in OUTPUT_RAW):
                    out.append(f"{where}: a creation flag outside the named creators")
            if isinstance(node, ast.Call) and _names_core(node.func):
                restorer = node.args[2] if len(node.args) == 3 else None
                core_calls.setdefault(f"{rel}::{qual}", []).append(getattr(restorer, "id", "?"))
            elif _names_core(node) and not _is_call_target(node, tree):
                out.append(f"{where}: _create_private_core used as a value")
    expected = {f"{OUTPUT}::{entry}": restorers for entry, restorers in PRIVATE_ENTRIES.items()}
    if core_calls != expected:
        out.append(f"_create_private_core callers {core_calls}, expected {expected}")
    return out


def _is_create_flag(rel: str, node: ast.AST, scope: Scope, index: ProjectIndex) -> bool:
    if rel == OUTPUT and isinstance(node, ast.Name) and node.id == "_FILE_CREATE":
        return True
    if not isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
        return False
    return resolve(node, scope, index) in CREATE_FLAGS


def _names_core(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "_create_private_core") or (
        isinstance(node, ast.Attribute) and node.attr == "_create_private_core"
    )


def _is_call_target(node: ast.AST, tree: ast.Module) -> bool:
    return any(isinstance(n, ast.Call) and n.func is node for n in ast.walk(tree))


def _flag_definition(tree: ast.Module) -> set[int]:
    """The ids of the nodes in the one module-level `_FILE_CREATE = ...` assignment."""
    defs = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and [getattr(t, "id", None) for t in n.targets] == ["_FILE_CREATE"]
    ]
    assert len(defs) == 1, "exactly one `_FILE_CREATE` definition"
    return {id(n) for n in ast.walk(defs[0])}


@pytest.mark.parametrize("cell", ["names", "open_name", "create_choke_point"])
def test_arena_prefix_used_only_by_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cell: str
) -> None:
    if cell == "names":
        assert prefix_findings(SRC) == []
    elif cell == "open_name":
        from decoy_engine.fileaccess._scratch import ARENA_PREFIX, ScratchArena

        assert ARENA_PREFIX == "decoy-arena-v1-"
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        with ScratchArena.open() as arena:
            name = os.path.basename(arena.local_path())
            assert SWEEP_NAME.fullmatch(name), name
            assert os.listdir(tmp_path) == [name]
    else:
        assert choke_point_findings(SRC) == []


# -- seeded bypasses: each must be seen --------------------------------------------------------

_OUTPUT = """import os

_FILE_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL


def _restore_always(fd):
    pass


def _restore_missing_owner_bits(fd):
    pass


def _create_private_core(dir_fd, name, restore):
    return os.open(name, _FILE_CREATE, 0o600, dir_fd=dir_fd)


def create_private_at(dir_fd, name):
    return _create_private_core(dir_fd, name, {always})


def create_private_child_at(dir_fd, name):
    return _create_private_core(dir_fd, name, {child})


class RootedOutputDir:
    def begin(self, fd):
        {begin}
"""
_CLEAN = {"always": "_restore_always", "child": "_restore_missing_owner_bits", "begin": "pass"}
_SEEDS = {
    "arena_mkdir": "import os\n\n\ndef create_dir(fd, n):\n    os.mkdir(n, 0o700, dir_fd=fd)\n",
    "arena_mkdtemp_alias": "from tempfile import mkdtemp as fresh\n\n\ndef root():\n    fresh()\n",
    "arena_chmod_getattr": "import os\n\n\ndef fix(p):\n    getattr(os, 'chmod')(p, 0o700)\n",
    "arena_fchmod": "import os\n\n\ndef fix(fd):\n    os.fchmod(fd, 0o700)\n",
    "arena_raw_marker": (
        "import os\nfrom os import O_CREAT as C\n\n\ndef marker(fd):\n"
        "    return os.open('.m', os.O_WRONLY | C, 0o600, dir_fd=fd)\n"
    ),
}
_OUTPUT_SEEDS = {
    "output_bare_mkdir": {"begin": "os.mkdir('x', 0o700, dir_fd=fd)"},
    "output_second_create": {"begin": "return os.open('x', _FILE_CREATE, 0o600, dir_fd=fd)"},
    "output_swapped_restorers": {
        "always": "_restore_missing_owner_bits",
        "child": "_restore_always",
    },
    "output_core_as_value": {"begin": "return _create_private_core"},
}


def _seed(root: Path, files: dict[str, str]) -> Path:
    pkg = root / "decoy_engine" / "fileaccess"
    pkg.mkdir(parents=True)
    (pkg.parent / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    for rel, body in files.items():
        (pkg.parent / rel).write_text(body)
    return root


@pytest.mark.parametrize("cell", sorted([*_SEEDS, *_OUTPUT_SEEDS, "prefix_literal"]))
def test_a_seeded_bypass_is_seen(tmp_path: Path, cell: str) -> None:
    clean = {"fileaccess/_output.py": _OUTPUT.format(**_CLEAN)}
    assert choke_point_findings(_seed(tmp_path / "clean", clean)) == []  # non-vacuity
    files = dict(clean)
    if cell == "prefix_literal":
        files["isolated.py"] = (
            "import tempfile\n\n\ndef run():\n    tempfile.mkdtemp(prefix='decoy-arena-v1-x')\n"
        )
        assert prefix_findings(_seed(tmp_path / "seeded", files)), "the literal went unseen"
        return
    if cell in _SEEDS:
        files["fileaccess/_scratch.py"] = _SEEDS[cell]
    else:
        files["fileaccess/_output.py"] = _OUTPUT.format(**{**_CLEAN, **_OUTPUT_SEEDS[cell]})
    assert choke_point_findings(_seed(tmp_path / "seeded", files)), f"{cell} went unseen"
