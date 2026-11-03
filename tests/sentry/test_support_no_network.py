"""F32 T-E9: no module of `decoy_engine.support` imports a network client.

A support bundle is never uploaded by Decoy; the operator reviews and sends the file. The AST
walk covers top-level, nested and `from` imports, plus `__import__` / `import_module` with a
constant name, so a later edit cannot add an upload path without this sentry going red.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / "src" / "decoy_engine" / "support"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "support_no_network" / "uploads.py"
FORBIDDEN = (
    "socket",
    "http",
    "urllib",
    "requests",
    "httpx",
    "boto3",
    "botocore",
    "google.cloud",
    "azure",
    "smtplib",
    "ftplib",
)


def _forbidden(name: str) -> bool:
    return any(name == f or name.startswith(f + ".") for f in FORBIDDEN)


def network_imports(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Call) and node.args:
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            arg = node.args[0]
            if called in ("__import__", "import_module") and isinstance(arg, ast.Constant):
                names = [str(arg.value)]
        hits.extend((node.lineno, n) for n in names if _forbidden(n))
    return sorted(set(hits))


def test_no_network_imports() -> None:
    modules = sorted(SUPPORT.rglob("*.py"))
    assert SUPPORT / "__init__.py" in modules, "decoy_engine.support package is missing"
    found = {str(p.relative_to(ROOT)): network_imports(p) for p in modules}
    assert {k: v for k, v in found.items() if v} == {}


def test_seeded_fixture_is_red() -> None:
    names = {n for _, n in network_imports(FIXTURE)}
    assert {"urllib.request", "http", "socket", "google.cloud"} <= names
