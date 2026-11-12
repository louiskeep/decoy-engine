"""Config column lookups the auto-chunk planner reads (split out of `_planner` for its size bar).

`_planner` imports these under their historical private names.
"""

from __future__ import annotations

from typing import Any


def _table_column_entries(config: dict[str, Any], *, table: str) -> list[dict[str, Any]]:
    """The dict column entries of `table` in the raw config, else []."""
    tables = config.get("tables") or []
    table_cfg = next((t for t in tables if isinstance(t, dict) and t.get("name") == table), None)
    if table_cfg is None:
        return []
    return [c for c in table_cfg.get("columns") or [] if isinstance(c, dict)]


def _bucketize_columns(config: dict[str, Any], *, table: str) -> list[str]:
    return sorted(
        str(c.get("name", "?"))
        for c in _table_column_entries(config, table=table)
        if c.get("strategy") == "bucketize"
    )


def _fpe_join_group_columns(config: dict[str, Any], *, table: str) -> list[str]:
    """Columns on `table` whose fpe config sets `fpe_join_group`."""
    tables = config.get("tables") or []
    table_cfg = next((t for t in tables if isinstance(t, dict) and t.get("name") == table), None)
    if table_cfg is None:
        return []
    out: list[str] = []
    for col_entry in table_cfg.get("columns") or []:
        if not isinstance(col_entry, dict) or col_entry.get("strategy") != "fpe":
            continue
        if (col_entry.get("provider_config") or {}).get("fpe_join_group"):
            out.append(str(col_entry.get("name", "?")))
    return sorted(out)
