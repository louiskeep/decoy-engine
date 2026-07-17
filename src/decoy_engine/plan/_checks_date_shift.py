"""Plan-compile check for date_shift `group_by` columns (HC-3a).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling. See the SP-10 comment in tests/sentry/test_module_size.py: the
_checks.py module decomposes into per-strategy sub-modules as new strategies
land; this is the HC-3a slice for date_shift.

This module exports exactly one function: ``check_date_shift_group_by_refs``,
which validates that date_shift columns using ``group_by`` name an existing
entity column. It is imported by plan/_compile.py alongside the other check
functions from plan/_checks.py and sibling _checks_*.py modules.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def check_date_shift_group_by_refs(config: dict[str, Any]) -> None:
    """Reject date_shift columns whose `group_by` column is missing.

    Compile-check ownership table row #27 (HC-3a, 2026-07-17). One failure
    mode caught here (plan-compile time, before any execution):

    Missing group_by ref: the ``group_by`` key (in ``provider_config``)
    names a column not present in the same table. A missing ref is
    guaranteed to raise KeyError at execution time; rejecting here surfaces
    it with the exact missing name.

    date_shift is mask-kind only (no generate-kind `type: date_shift`), so
    unlike windowed_date/group_key this check has a single loop.

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

        # Build the set of column names known in this table. date_shift is
        # mask-kind only, so a group_by anchor must be a real (masked/source)
        # `columns` entry present in the frame at mask time -- deliberately NOT
        # unioning `generate_columns` (unlike windowed_date, which can anchor on
        # a generate-side column). Including them would let a group_by naming a
        # generate-only column pass compile and then KeyError at execution;
        # restricting to `columns` lets compile fully own the validation.
        all_col_names: set[str] = set()
        for col_entry in table_entry.get("columns", []) or []:
            if isinstance(col_entry, dict) and col_entry.get("name"):
                all_col_names.add(str(col_entry["name"]))

        # Check mask-kind columns (strategy: date_shift).
        for col_entry in table_entry.get("columns") or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "date_shift":
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config") or {}
            if not isinstance(pc, dict):
                continue
            group_by = pc.get("group_by")
            if group_by and str(group_by) not in all_col_names:
                raise PlanCompileError(
                    code="date_shift_missing_group_by_ref",
                    path=(f"tables.{table_name}.columns.{col_name}.provider_config.group_by"),
                    message=(
                        f"date_shift column {col_name!r} in table "
                        f"{table_name!r} references group_by column "
                        f"{group_by!r} which is not defined in the same "
                        f"table. Available columns: {sorted(all_col_names)!r}."
                    ),
                )
