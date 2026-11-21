"""Sentry: the vault crypto and format live in one module (`vault/_format.py`).

`nacl` is imported only there, the vault key label appears once in `src/`, and the vault package does not import
`cryptography.fernet` outside `_v2_reader.py` (the read-only v2 cipher; the secret-provider Fernet key requirement is a different use). The v2 key label appears once, there. The scan resolves `import x as y`, `from x import y` and
`importlib` strings, so the realistic ways of reaching the cipher from another module are caught.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
OWNER = "decoy_engine/vault/_format.py"
V2_OWNER = "decoy_engine/vault/_v2_reader.py"
KEY_LABEL = b"vault-key/v2"
V2_KEY_LABEL = b"vault-key/v1"


def _imports(tree: ast.AST) -> set[str]:
    """Every dotted module name the tree imports, by statement or by `importlib` string."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            if callee.endswith(("import_module", "__import__")) and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
    return found


def _label_count(tree: ast.AST, label: bytes) -> int:
    return sum(1 for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == label)


def scan(sources: dict[str, str]) -> list[str]:
    """Violations over `{relative path: source}`."""
    problems: list[str] = []
    label_count = v2_label_count = 0
    for rel, text in sources.items():
        tree = ast.parse(text)
        modules = _imports(tree)
        if rel != OWNER and any(m == "nacl" or m.startswith("nacl.") for m in modules):
            problems.append(f"{rel}: imports nacl")
        if (
            rel.startswith("decoy_engine/vault/")
            and rel != V2_OWNER
            and any(m.startswith("cryptography.fernet") for m in modules)
        ):
            problems.append(f"{rel}: imports cryptography.fernet")
        label_count += _label_count(tree, KEY_LABEL)
        count = _label_count(tree, V2_KEY_LABEL)
        v2_label_count += count
        if count and rel != V2_OWNER:
            problems.append(f"{rel}: holds the v2 vault key label")
    if label_count != 1:
        problems.append(f"the vault key label appears {label_count} times, not once")
    if v2_label_count != 1:
        problems.append(f"the v2 vault key label appears {v2_label_count} times, not once")
    return problems


def _source() -> dict[str, str]:
    return {
        str(p.relative_to(SRC)): p.read_text(encoding="utf-8")
        for p in (SRC / "decoy_engine").rglob("*.py")
    }


def test_the_tree_holds() -> None:
    assert scan(_source()) == []


def test_the_owner_module_is_where_nacl_is_imported() -> None:
    assert any(m.startswith("nacl") for m in _imports(ast.parse(_source()[OWNER])))


def _seeded(extra_path: str, extra_text: str) -> list[str]:
    sources = dict(_source())
    sources[extra_path] = extra_text
    return scan(sources)


def test_seeded_nacl_import_elsewhere_fails() -> None:
    assert _seeded("decoy_engine/vault/_reader.py", "import nacl.bindings\n")
    assert _seeded("decoy_engine/x.py", "from nacl import bindings as b\n")
    assert _seeded(
        "decoy_engine/x.py", "import importlib\nimportlib.import_module('nacl.bindings')\n"
    )


def test_seeded_fernet_import_fails() -> None:
    assert _seeded("decoy_engine/vault/_x.py", "from cryptography.fernet import Fernet\n")


def test_seeded_second_key_label_fails() -> None:
    assert _seeded("decoy_engine/x.py", "LABEL = b'vault-key/v2'\n")


def test_v2_owner_may_import_fernet_and_nothing_else_may() -> None:
    sources = dict(_source())
    sources[V2_OWNER] += "\nfrom cryptography.fernet import Fernet\n"
    assert scan(sources) == []
    assert any(
        "imports cryptography.fernet" in p
        for p in _seeded("decoy_engine/vault/_reader.py", "import cryptography.fernet\n")
    )


def test_seeded_second_v2_key_label_fails() -> None:
    assert _seeded("decoy_engine/vault/_reader.py", "LABEL = b'vault-key/v1'\n")
