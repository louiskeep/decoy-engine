"""R1b: each operator default lives in the parameter resolver, not in a route adapter.

The three adapter modules are scanned for the literals and calls the resolver now owns. A
literal reappearing in one of them is a second copy of a default that the other route would
not see. Admission, the schema rule, the kernels, the oracle and the out-of-core route keep
their own checks and are out of scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[2] / "src" / "decoy_engine" / "execution"
ADAPTERS = (
    "physical/_shadow_operators.py",
    "physical/_shadow_bindings.py",
    "native/_chunk_masking.py",
)


def _offences(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            if node.value == "REDACTED" or node.value == "month":
                found.append(f"line {node.lineno}: literal {node.value!r}")
            elif node.value == 16 and not isinstance(node.value, bool):
                found.append(f"line {node.lineno}: literal 16")
        elif isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if (
                isinstance(head, ast.Constant)
                and isinstance(head.value, str)
                and head.value.startswith("group_key/")
            ):
                found.append(f"line {node.lineno}: f-string {head.value!r}")
        elif isinstance(node, ast.Name) and node.id in {"DEFAULT_MIN_DAYS", "DEFAULT_MAX_DAYS"}:
            found.append(f"line {node.lineno}: name {node.id}")
        elif isinstance(node, ast.ImportFrom):
            found.extend(
                f"line {node.lineno}: import {a.name}"
                for a in node.names
                if a.name in {"DEFAULT_MIN_DAYS", "DEFAULT_MAX_DAYS", "_resolve_truncate_keep"}
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_resolve_truncate_keep"
        ):
            found.append(f"line {node.lineno}: call _resolve_truncate_keep")
    return found


@pytest.mark.parametrize("module", ADAPTERS)
def test_adapter_holds_no_operator_default(module: str) -> None:
    tree = ast.parse((ENGINE / module).read_text(encoding="utf-8"))
    assert _offences(tree) == []
