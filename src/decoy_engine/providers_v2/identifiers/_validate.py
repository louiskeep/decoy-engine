"""deterministic_namespace_completeness: compile-check row 9.

Per S6 spec §6 + cross-sprint contracts §4 row 9: every column with
`deterministic: true` MUST declare a namespace. Distinct from row 1
(`namespace_ambiguity` which catches same-column-in-two-namespaces);
row 9 catches deterministic-mode-without-namespace.

Shared with S7 per cross-sprint contracts §4 row 9. S10 consolidates
the check registry per R11.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def deterministic_namespace_completeness(config: dict[str, Any]) -> None:
    """Compile-check row 9. Raises on missing namespace + deterministic.

    Walks the config tables; for each column with `deterministic: true`
    (or `allow_collisions: true`, the gap-closure item 2 alias that implies
    deterministic) and missing/empty `namespace`, raises
    `PlanCompileError(code='deterministic_namespace_missing')`.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            # `allow_collisions: true` (item 2) is a compile-time alias for
            # deterministic + reuse, so it carries the same namespace
            # requirement (the deterministic derive key).
            implies_deterministic = bool(col_entry.get("deterministic", False)) or bool(
                col_entry.get("allow_collisions", False)
            )
            if not implies_deterministic:
                continue
            namespace = col_entry.get("namespace")
            if not namespace:
                col_name = col_entry.get("name", "?")
                trigger = (
                    "`deterministic: true`"
                    if col_entry.get("deterministic", False)
                    else "`allow_collisions: true`"
                )
                raise PlanCompileError(
                    code="deterministic_namespace_missing",
                    path=f"tables.{table_name}.columns.{col_name}",
                    message=(
                        f"Column {table_name}.{col_name} declares "
                        f"{trigger} but does not declare a namespace. "
                        "Deterministic mode requires an explicit namespace to "
                        "guarantee cross-column consistency."
                    ),
                )
