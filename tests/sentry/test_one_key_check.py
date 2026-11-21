"""Sentry: one key check value construction (`internal/crypto.key_check_value`).

A 16-character truncation of an HMAC result, and any `key-check` string constant,
appears only in `internal/crypto.py`. The resume store and the vault both go through it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
OWNER = "decoy_engine/internal/crypto.py"
_MACS = ("hmac_hex", "hmac.new")
# The registry of frozen derivation labels names each label once; it builds no check value.
_LABEL_REGISTRY = "decoy_engine/contracts/stability.py"


def _is_mac(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and any(m in ast.unparse(node.func) for m in _MACS)


def _is_check_slice(node: ast.Subscript) -> bool:
    upper = node.slice.upper if isinstance(node.slice, ast.Slice) else None
    if isinstance(upper, ast.Constant):
        return upper.value == 16
    return isinstance(upper, ast.Name) and "KEY_CHECK" in upper.id


def _mac_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_mac(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def scan(sources: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for rel, text in sources.items():
        if rel == OWNER:
            continue
        skip_labels = rel == _LABEL_REGISTRY
        tree = ast.parse(text)
        names = _mac_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and _is_check_slice(node):
                value = node.value
                if _is_mac(value) or (isinstance(value, ast.Name) and value.id in names):
                    problems.append(f"{rel}:{node.lineno}: a second key check construction")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "key-check" in node.value
                and not skip_labels
            ):
                problems.append(f"{rel}:{node.lineno}: a key check label outside {OWNER}")
    return problems


def _source() -> dict[str, str]:
    return {
        str(p.relative_to(SRC)): p.read_text(encoding="utf-8")
        for p in (SRC / "decoy_engine").rglob("*.py")
    }


def test_the_tree_holds() -> None:
    assert scan(_source()) == []


def test_seeded_direct_slice_fails() -> None:
    assert scan({"decoy_engine/x.py": "d = hmac_hex(k, 'l')[:16]\n"})


def test_seeded_named_digest_slice_fails() -> None:
    assert scan({"decoy_engine/x.py": "d = hmac_hex(k, 'l')\nc = d[:_KEY_CHECK_HEX]\n"})


def test_seeded_label_constant_fails() -> None:
    assert scan({"decoy_engine/x.py": "L = 'my-key-check/v1'\n"})
