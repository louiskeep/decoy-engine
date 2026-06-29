"""Plan-compile check for group_key columns (SP-10c).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling. See the SP-10 comment in tests/sentry/test_module_size.py: the
_checks.py module decomposes into per-strategy sub-modules as new strategies
land; this is the SP-10c slice for group_key.

This module exports exactly one function: ``check_group_key_refs``,
which validates that group_key columns name an existing group_by column.
It is imported by plan/_compile.py alongside the other check functions from
plan/_checks.py and sibling _checks_*.py modules.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def check_group_key_refs(config: dict[str, Any]) -> None:
    """Reject group_key columns whose group_by column is missing.

    Compile-check ownership table row #20 (SP-10c / P5.P.group_key,
    2026-06-29). One failure mode caught here (plan-compile time, before any
    execution):

    Missing group_by ref: the ``group_by`` key names a column not present in
    the same table. A missing ref is guaranteed to raise KeyError at execution
    time; rejecting here surfaces it with the exact missing name.

    Covers both mask-kind columns (strategy: group_key with
    provider_config.group_by) and generate-kind columns (type: group_key
    with group_by at the column level).

    Config-only (no profile, no source data): safe to run in both compile
    branches and in ``run_config_only_checks``. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: The group_by column ref is missing from the same
            table.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")

        # Build the full set of column names known in this table.
        all_col_names: set[str] = set()
        for col_entry in table_entry.get("columns", []) or []:
            if isinstance(col_entry, dict) and col_entry.get("name"):
                all_col_names.add(str(col_entry["name"]))
        for col_entry in table_entry.get("generate_columns", []) or []:
            if isinstance(col_entry, dict) and col_entry.get("name"):
                all_col_names.add(str(col_entry["name"]))

        # Check mask-kind columns (strategy: group_key).
        for col_entry in table_entry.get("columns") or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "group_key":
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config") or {}
            if not isinstance(pc, dict):
                continue
            group_by = pc.get("group_by")
            if group_by and str(group_by) not in all_col_names:
                raise PlanCompileError(
                    code="group_key_missing_group_by_ref",
                    path=(f"tables.{table_name}.columns.{col_name}.provider_config.group_by"),
                    message=(
                        f"group_key column {col_name!r} in table "
                        f"{table_name!r} references group_by column "
                        f"{group_by!r} which is not defined in the same "
                        f"table. Available columns: {sorted(all_col_names)!r}."
                    ),
                )

        # Check generate-kind columns (type: group_key).
        for col_entry in table_entry.get("generate_columns") or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("type") != "group_key":
                continue
            col_name = col_entry.get("name", "?")
            group_by = col_entry.get("group_by")
            if group_by and str(group_by) not in all_col_names:
                raise PlanCompileError(
                    code="group_key_missing_group_by_ref",
                    path=(f"tables.{table_name}.generate_columns.{col_name}.group_by"),
                    message=(
                        f"group_key column {col_name!r} in table "
                        f"{table_name!r} references group_by column "
                        f"{group_by!r} which is not defined in the same "
                        f"table. Available columns: {sorted(all_col_names)!r}."
                    ),
                )
