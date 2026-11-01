"""Sentry: the engine's HKDF delegates to PyCA `cryptography`, not a hand-written loop.

The module `determinism/_hkdf.py` keeps its three entry points (callers and the empty-salt rule
are unchanged) but the RFC 5869 arithmetic is PyCA's. A stdlib `hmac`/`hashlib` import or a
`bytes([i])` block counter in the module is a hand-rolled HKDF coming back.
"""

from __future__ import annotations

import ast
from pathlib import Path

HKDF_MODULE = (
    Path(__file__).resolve().parents[2] / "src" / "decoy_engine" / "determinism" / "_hkdf.py"
)


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_hkdf_module_delegates_to_cryptography() -> None:
    tree = ast.parse(HKDF_MODULE.read_text(encoding="utf-8"))
    imports = _imports(tree)
    assert "hmac" not in imports and "hashlib" not in imports, sorted(imports)
    assert "cryptography.hazmat.primitives.kdf.hkdf" in imports, sorted(imports)
    loops = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]
    assert not loops, "a loop in _hkdf.py is an expand step written by hand"
