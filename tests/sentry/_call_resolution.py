"""Resolve the local spellings of a called function in one module's AST, for sentries.

A call of `target` counts when it is spelled as the bare name, as an alias from
`from m import target as alias`, as a plain re-binding of either (`alias2 = alias`, `f = mod.target`), or as an
attribute (`mod.target(...)`, which also covers `import m as mod`). Matching is by name, not by
the module a name was imported from, so two functions sharing a name are not told apart.
"""

from __future__ import annotations

import ast


def local_names(tree: ast.AST, target: str) -> set[str]:
    """Names `target` is reachable under in a module: itself, import aliases, plain re-bindings."""
    names = {target}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname for a in node.names if a.name == target and a.asname)
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and (
                (isinstance(node.value, ast.Name) and node.value.id in names)
                or (isinstance(node.value, ast.Attribute) and node.value.attr in names)
            ):
                bound = {t.id for t in node.targets if isinstance(t, ast.Name)} - names
                if bound:
                    names |= bound
                    grew = True
    return names


def resolved_calls(tree: ast.AST, target: str) -> list[ast.Call]:
    """Calls of `target` by its name, an alias of it, or as an attribute (`mod.target(...)`)."""
    names = local_names(tree, target)
    return [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Name) and call.func.id in names)
            or (isinstance(call.func, ast.Attribute) and call.func.attr == target)
        )
    ]
