"""Construction-enforced sentry for the fd-only filesystem invariant (argon).

The defect class the 3-round TOCTOU cap exposed: once a directory or file is
verified against its expected kernel identity through an ``O_NOFOLLOW``
descriptor, no later filesystem syscall may re-resolve a multi-component path
through the ambient namespace; every post-check call must run on the verified
``fd`` or via a single-component ``*at(..., dir_fd=...)`` anchored to it.

This sentry parses the AST of every module under ``src/decoy_engine/scratch/``
and fails the build on an unsanctioned path-based filesystem operation, so the
class cannot regress. It is a drift guard (static analysis cannot prove arbitrary
dynamic Python safe); the runtime proof is the use-time TOCTOU suite plus the
real-kernel suite on the quota host.

The scanner is proven non-vacuous: a self-test feeds the exact snippets the cap
was about (``record.path.exists()``, ``shutil.rmtree(arena)`` with no ``dir_fd``,
``os.stat(self._root)``, ``def reclaim(self, arena: Path)``) and asserts each is
flagged.
"""

from __future__ import annotations

import ast
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRATCH_DIR = HERE.parents[1] / "src" / "decoy_engine" / "scratch"

# Fully-qualified os/shutil/tempfile call tails that re-resolve a path string.
_FORBIDDEN_OUTRIGHT = frozenset(
    {
        "os.walk",
        "os.chmod",
        "os.stat",
        "os.makedirs",
        "os.removedirs",
        "os.path.realpath",
        "os.path.exists",
        "os.path.isdir",
        "os.path.islink",
        "os.path.samestat",
        "shutil.copytree",
    }
)
# These are allowed only with a non-None ``dir_fd=`` keyword.
_REQUIRE_DIR_FD = frozenset(
    {
        "os.mkdir",
        "os.lstat",
        "os.unlink",
        "os.rmdir",
        "os.rename",
        "os.replace",
        "shutil.rmtree",
    }
)
# Path methods that re-resolve the path; unambiguous as filesystem ops.
_FORBIDDEN_PATH_METHODS = frozenset(
    {
        "exists",
        "is_dir",
        "is_file",
        "is_symlink",
        "resolve",
        "iterdir",
        "glob",
        "rglob",
        "samestat",
        "lstat",
        "rmdir",
    }
)
# tempfile constructors that create under a path string.
_FORBIDDEN_TEMPFILE = frozenset(
    {
        "tempfile.mkdtemp",
        "tempfile.mkstemp",
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryDirectory",
        "tempfile.TemporaryFile",
    }
)

# (basename, function name): pre-activation / bootstrap helpers argon sanctions.
# The _seam fallbacks are the unmanaged pre-activation path (identity None); the
# _ledger functions are the control-plane SQLite bootstrap under the verified
# control arena.
_ALLOWLIST = frozenset(
    {
        ("_seam.py", "__init__"),
        ("_seam.py", "allocate_native_dir"),
        ("_seam.py", "allocate_file"),
        ("_seam.py", "release_all"),
        ("_seam.py", "as_native_dir"),
        ("_seam.py", "ensure_native_dir"),
        ("_seam.py", "ensure_publication_stage"),
        ("_seam.py", "ensure_publication_file"),
        ("_seam.py", "ensure_scratch_file"),
        ("_ledger.py", "bootstrap"),
        ("_ledger.py", "_verify_single_device"),
    }
)

_FORBIDDEN_DEF_NAMES = frozenset({"verify_target_identity", "_verify_capability_target"})

# Attribute chains rooted at one of these are module calls (e.g. os.lstat,
# shutil.rmtree), classified by their qualified tail above, never a Path method.
_STDLIB_MODULES = frozenset(
    {"os", "shutil", "tempfile", "fcntl", "struct", "secrets", "sqlite3", "uuid", "getpass", "stat"}
)


def _call_tail(node: ast.Call) -> str:
    parts: list[str] = []
    cur: ast.expr = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _kw(node: ast.Call, name: str) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _has_dir_fd(node: ast.Call) -> bool:
    value = _kw(node, "dir_fd")
    return value is not None and not (isinstance(value, ast.Constant) and value.value is None)


def _flags_contain(node: ast.Call, *names: str) -> bool:
    """True if the os.open flags argument (positional index 1) statically ORs in
    every one of ``names`` (e.g. O_NOFOLLOW and O_DIRECTORY)."""

    flags = node.args[1] if len(node.args) > 1 else _kw(node, "flags")
    if flags is None:
        return False
    seen = {n.attr for n in ast.walk(flags) if isinstance(n, ast.Attribute)}
    return all(name in seen for name in names)


def _is_proc_literal_open(node: ast.Call) -> bool:
    """True for ``open("/proc/...")``: reading a kernel pseudo-file, not a
    resource path (used to read mount options by device)."""

    if not node.args:
        return False
    first = node.args[0]
    return (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.startswith("/proc/")
    )


def _enclosing_func(stack: list[str]) -> str:
    return stack[-1] if stack else "<module>"


def _attr_root(func: ast.expr) -> str | None:
    """The root Name id of an attribute chain (``os`` for ``os.path.realpath``,
    ``record`` for ``record.path.exists``), or None."""

    cur: ast.expr = func
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    return cur.id if isinstance(cur, ast.Name) else None


def scan_calls(tree: ast.AST, basename: str) -> list[str]:
    """Rules A + B + open/tempfile, honoring the per-function allowlist."""

    findings: list[str] = []
    func_stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            func_stack.append(node.name)
            self.generic_visit(node)
            func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

        def visit_Call(self, node: ast.Call) -> None:
            self._check(node)
            self.generic_visit(node)

        def _check(self, node: ast.Call) -> None:
            func = _enclosing_func(func_stack)
            allowed = (basename, func) in _ALLOWLIST
            tail = _call_tail(node)
            leaf = tail.rsplit(".", 1)[-1]
            where = f"{basename}:{func}:L{node.lineno} {tail or leaf}"
            if tail in _FORBIDDEN_OUTRIGHT and not allowed:
                findings.append(f"{where} (path-based; no fd variant permitted)")
            elif tail in _REQUIRE_DIR_FD and not _has_dir_fd(node) and not allowed:
                findings.append(f"{where} (missing dir_fd=)")
            elif tail == "os.open" and not allowed:
                if not _has_dir_fd(node) and not _flags_contain(node, "O_NOFOLLOW", "O_DIRECTORY"):
                    findings.append(f"{where} (os.open without dir_fd or O_NOFOLLOW|O_DIRECTORY)")
            elif tail in _FORBIDDEN_TEMPFILE and not allowed:
                findings.append(f"{where} (tempfile path create)")
            elif tail == "open" and isinstance(node.func, ast.Name) and not allowed:
                if not _is_proc_literal_open(node):
                    findings.append(f"{where} (builtins open on a non-/proc path)")
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in _FORBIDDEN_PATH_METHODS
                and _attr_root(node.func) not in _STDLIB_MODULES
                and not allowed
            ):
                findings.append(f"{where} (Path.{node.func.attr}() re-resolves a path)")

    Visitor().visit(tree)
    return findings


def scan_forbidden_defs(tree: ast.AST, basename: str) -> list[str]:
    """Rule C: the ``-> None`` identity-check helpers must not exist (they were
    replaced by the ``open_verified_*`` context managers)."""

    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in _FORBIDDEN_DEF_NAMES:
            findings.append(f"{basename}:{node.name} (forbidden -> None identity-check helper)")
    return findings


def scan_provider_path_params(tree: ast.AST, basename: str) -> list[str]:
    """Rule D: no ``StorageProvider`` method parameter may be annotated ``Path``
    (parameters must be verified-descriptor handles or plain scalars)."""

    findings: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "StorageProvider"):
            continue
        for method in node.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            for arg in method.args.args + method.args.kwonlyargs:
                ann = arg.annotation
                is_path = (isinstance(ann, ast.Name) and ann.id == "Path") or (
                    isinstance(ann, ast.Attribute) and ann.attr == "Path"
                )
                if is_path:
                    findings.append(
                        f"{basename}:StorageProvider.{method.name}({arg.arg}: Path) "
                        "(provider method takes a bare Path for an existing resource)"
                    )
    return findings


def _scan_module(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    base = path.name
    return (
        scan_calls(tree, base)
        + scan_forbidden_defs(tree, base)
        + scan_provider_path_params(tree, base)
    )


def test_scratch_package_has_no_path_based_filesystem_ops() -> None:
    offenders: list[str] = []
    modules = sorted(SCRATCH_DIR.glob("*.py"))
    assert modules, "no scratch modules found to scan (sentry would be vacuous)"
    for module in modules:
        offenders.extend(_scan_module(module))
    assert not offenders, "fd-only filesystem invariant violated:\n" + "\n".join(offenders)


# --- Non-vacuity: the scanner flags each defect the cap was about ------------

_SEEDED = {
    "path_exists_pre_check": "def release(self):\n    return record.path.exists()\n",
    "rmtree_no_dir_fd": "import shutil\ndef reclaim(self):\n    shutil.rmtree(arena)\n",
    "os_stat_on_root": "import os\ndef _register(self):\n    return os.stat(self._root)\n",
    "os_open_no_fd": "import os\ndef f():\n    return os.open(p, os.O_WRONLY)\n",
    "tempfile_mkstemp": "import tempfile\ndef f():\n    return tempfile.mkstemp(dir=stage)\n",
    "os_walk": "import os\ndef f():\n    return list(os.walk(arena))\n",
    "path_resolve": "def f(p):\n    return p.resolve()\n",
}


def test_scanner_flags_each_seeded_call_violation() -> None:
    for name, src in _SEEDED.items():
        tree = ast.parse(src)
        assert scan_calls(tree, "seeded.py"), f"scanner is vacuous: missed {name!r}"


def test_scanner_flags_forbidden_identity_helper_def() -> None:
    tree = ast.parse("def verify_target_identity(path, a, b, m):\n    return None\n")
    assert scan_forbidden_defs(tree, "seeded.py")


def test_scanner_flags_provider_path_param() -> None:
    tree = ast.parse(
        "class StorageProvider:\n"
        "    def reclaim(self, arena: Path) -> tuple[bool, int, int]:\n"
        "        ...\n"
    )
    assert scan_provider_path_params(tree, "_provider.py")


def test_scanner_passes_a_clean_fd_relative_snippet() -> None:
    clean = (
        "import os\n"
        "def reclaim(target):\n"
        "    fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=pfd)\n"
        "    os.unlink(target.name, dir_fd=pfd)\n"
        "    os.rmdir(target.name, dir_fd=pfd)\n"
    )
    assert scan_calls(ast.parse(clean), "clean.py") == []


def test_scanner_allows_proc_literal_open() -> None:
    tree = ast.parse("def f():\n    return open('/proc/self/mountinfo')\n")
    assert scan_calls(tree, "_local_provider.py") == []
