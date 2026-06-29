"""Plan-compile check for windowed_date columns (SP-10c).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling. See the SP-10 comment in tests/sentry/test_module_size.py: the
_checks.py module decomposes into per-strategy sub-modules as new strategies
land; this is the SP-10c slice for windowed_date.

This module exports exactly one function: ``check_windowed_date_refs``,
which validates that windowed_date columns name an existing anchor column.
It is imported by plan/_compile.py alongside the other check functions from
plan/_checks.py and sibling _checks_*.py modules.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def check_windowed_date_refs(config: dict[str, Any]) -> None:
    """Reject windowed_date columns whose anchor column is missing.

    Compile-check ownership table row #19 (SP-10c / P5.S.windowed_date,
    2026-06-29). One failure mode caught here (plan-compile time, before any
    execution):

    Missing anchor ref: the ``anchor`` key names a column not present in the
    same table. A missing ref is guaranteed to raise KeyError at execution
    time; rejecting here surfaces it with the exact missing name.

    Covers both mask-kind columns (strategy: windowed_date with
    provider_config.anchor) and generate-kind columns (type: windowed_date
    with anchor at the column level).

    Config-only (no profile, no source data): safe to run in both compile
    branches and in ``run_config_only_checks``. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: The anchor column ref is missing from the same table.
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

        # Check mask-kind columns (strategy: windowed_date).
        for col_entry in table_entry.get("columns") or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "windowed_date":
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config") or {}
            if not isinstance(pc, dict):
                continue
            anchor = pc.get("anchor")
            if anchor and str(anchor) not in all_col_names:
                raise PlanCompileError(
                    code="windowed_date_missing_anchor_ref",
                    path=(f"tables.{table_name}.columns.{col_name}.provider_config.anchor"),
                    message=(
                        f"windowed_date column {col_name!r} in table "
                        f"{table_name!r} references anchor column "
                        f"{anchor!r} which is not defined in the same "
                        f"table. Available columns: {sorted(all_col_names)!r}."
                    ),
                )

        # Check generate-kind columns (type: windowed_date).
        for col_entry in table_entry.get("generate_columns") or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("type") != "windowed_date":
                continue
            col_name = col_entry.get("name", "?")
            anchor = col_entry.get("anchor")
            if anchor and str(anchor) not in all_col_names:
                raise PlanCompileError(
                    code="windowed_date_missing_anchor_ref",
                    path=(f"tables.{table_name}.generate_columns.{col_name}.anchor"),
                    message=(
                        f"windowed_date column {col_name!r} in table "
                        f"{table_name!r} references anchor column "
                        f"{anchor!r} which is not defined in the same "
                        f"table. Available columns: {sorted(all_col_names)!r}."
                    ),
                )
