"""Plan-compile check for derived_aggregate columns (SP-10b).

Added as its own module to avoid growing plan/_checks.py past its size
ceiling. See the SP-10 comment in tests/sentry/test_module_size.py: the
_checks.py module decomposes into per-strategy sub-modules as new strategies
land; this is the SP-10b slice.

This module exports exactly one function: ``check_derived_aggregate_refs``,
which validates that derived_aggregate columns name a valid op and an existing
source column. It is imported by plan/_compile.py alongside the other check
functions from plan/_checks.py.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def check_derived_aggregate_refs(config: dict[str, Any]) -> None:
    """Reject derived_aggregate columns whose source column is missing or op is invalid.

    Compile-check ownership table row #17 (SP-10b / P5.S.derived_aggregate,
    2026-06-28). Two failure modes caught here (plan-compile time, before any
    execution):

    1. Invalid op: the ``op`` key must be one of the AGGREGATE_OPS closed set.
       An unrecognised op is guaranteed dead at run time because DerivedAggregateConfig
       raises PlanCompileError there too, but surfacing it at compile time gives the
       operator a clearer error before a long run.

    2. Missing column ref: the ``column`` key names a source column that is not
       present in the same table. A missing ref is guaranteed to raise KeyError
       at execution time; rejecting here surfaces it with the exact missing name.

    Config-only (no profile, no source data): safe to run in both compile
    branches and in ``run_config_only_checks``. Validation never mutates
    (per engine rule).

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: An invalid op or missing source column is found.
    """
    from decoy_engine.transforms.derived_aggregate import AGGREGATE_OPS

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

        # Check mask-kind columns (strategy: derived_aggregate).
        for col_entry in table_entry.get("columns") or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "derived_aggregate":
                continue
            col_name = col_entry.get("name", "?")
            pc = col_entry.get("provider_config") or {}
            if not isinstance(pc, dict):
                continue
            op = pc.get("op")
            if op and str(op) not in AGGREGATE_OPS:
                raise PlanCompileError(
                    code="derived_aggregate_op_invalid",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.op",
                    message=(
                        f"derived_aggregate column {col_name!r} in table "
                        f"{table_name!r} has invalid op {op!r}. "
                        f"Allowed values: {sorted(AGGREGATE_OPS)!r}."
                    ),
                )
            source_col = pc.get("column")
            if source_col and str(source_col) not in all_col_names:
                raise PlanCompileError(
                    code="derived_aggregate_missing_column_ref",
                    path=(f"tables.{table_name}.columns.{col_name}.provider_config.column"),
                    message=(
                        f"derived_aggregate column {col_name!r} in table "
                        f"{table_name!r} references source column "
                        f"{source_col!r} which is not defined in the same "
                        f"table. Available columns: {sorted(all_col_names)!r}."
                    ),
                )

        # Check generate-kind columns (type: derived_aggregate).
        for col_entry in table_entry.get("generate_columns") or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("type") != "derived_aggregate":
                continue
            col_name = col_entry.get("name", "?")
            op = col_entry.get("op")
            if op and str(op) not in AGGREGATE_OPS:
                raise PlanCompileError(
                    code="derived_aggregate_op_invalid",
                    path=f"tables.{table_name}.generate_columns.{col_name}.op",
                    message=(
                        f"derived_aggregate column {col_name!r} in table "
                        f"{table_name!r} has invalid op {op!r}. "
                        f"Allowed values: {sorted(AGGREGATE_OPS)!r}."
                    ),
                )
            source_col = col_entry.get("column")
            if source_col and str(source_col) not in all_col_names:
                raise PlanCompileError(
                    code="derived_aggregate_missing_column_ref",
                    path=(f"tables.{table_name}.generate_columns.{col_name}.column"),
                    message=(
                        f"derived_aggregate column {col_name!r} in table "
                        f"{table_name!r} references source column "
                        f"{source_col!r} which is not defined in the same "
                        f"table. Available columns: {sorted(all_col_names)!r}."
                    ),
                )
