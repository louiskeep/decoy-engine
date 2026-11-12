"""Plan-time check: a generated relationship parent needs a key the plan can show is unique.

A child key resolves against the parent's key column. When the parent is generated and its key
repeats, one child value matches several parent rows and a post-run key check can only report the
duplicates after the fact. Joins in SQL need a ``PRIMARY KEY`` or ``UNIQUE`` constraint on the
parent side for the same reason, so the plan asks for the equivalent proof up front.

What counts as proof, per relationship parent that is a generate table: a ``row_count`` of at
most one; a ``sequence`` column with a non-zero ``step`` among the key columns; a column with
``unique: true`` among them; or a ``unique_together`` entry whose names, as a set, are exactly the
declared key columns. Anything else is refused with the table, the columns and the fix. A masked
or sourced parent is not this rule's business, and no submitted value reaches the message.
"""

from __future__ import annotations

from typing import Any

from decoy_engine import reasons
from decoy_engine.plan._errors import PlanCompileError

__all__ = ["check_generated_parent_keys"]


def _proves_unique(column: dict[str, Any]) -> bool:
    if column.get("unique") is True:
        return True
    return column.get("type") == "sequence" and column.get("step", 1) != 0


def _key_is_provable(table: dict[str, Any], keys: list[str], generated: dict[str, Any]) -> bool:
    rows = table.get("row_count")
    if isinstance(rows, int) and rows <= 1:
        return True
    if any(_proves_unique(generated[name]) for name in keys if name in generated):
        return True
    wanted = set(keys)
    return any(
        isinstance(pair, list) and set(pair) == wanted
        for pair in table.get("unique_together") or []
    )


def check_generated_parent_keys(config: dict[str, Any]) -> None:
    """Refuse a generate table used as a relationship parent without a provably unique key.

    Raises ``PlanCompileError`` on the first such parent, naming table and key columns.
    """
    tables, relationships = config.get("tables"), config.get("relationships")
    if not isinstance(tables, list) or not isinstance(relationships, list):
        return
    by_name = {t["name"]: t for t in tables if isinstance(t, dict) and "name" in t}
    for relationship in relationships:
        parent = relationship.get("parent") if isinstance(relationship, dict) else None
        if not isinstance(parent, dict):
            continue
        table = by_name.get(parent.get("table"))
        keys = parent.get("columns")
        if table is None:
            continue
        columns = table.get("generate_columns")
        if not isinstance(columns, list) or not isinstance(keys, list) or not keys:
            continue
        generated = {c["name"]: c for c in columns if isinstance(c, dict) and "name" in c}
        named = [k for k in keys if k in generated]
        if not named or _key_is_provable(table, keys, generated):
            continue
        summary = reasons.lookup(str(reasons.plan.RELATIONSHIP_PARENT_NOT_UNIQUE))[0].summary
        shown = ", ".join(repr(k) for k in keys)
        raise PlanCompileError(
            code=reasons.plan.RELATIONSHIP_PARENT_NOT_UNIQUE,
            path=f"tables.{parent['table']}.generate_columns.{named[0]}",
            message=(
                f"{summary} At table {parent['table']!r}, key columns {shown}. "
                "Set `unique: true` on the key column or use a `sequence`."
            ),
        )
